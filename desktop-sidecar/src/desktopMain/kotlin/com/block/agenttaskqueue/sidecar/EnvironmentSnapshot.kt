package com.block.agenttaskqueue.sidecar

import java.nio.file.Path
import java.nio.file.Paths
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

private val DEFAULT_QUEUE_DATA_DIR: Path = Paths.get("/tmp/agent-task-queue").toAbsolutePath().normalize()
private const val COMMAND_TIMEOUT_SECONDS = 5L
private val EMULATOR_SERIAL_PATTERN = Regex("^(?:emu|emulator)-(\\d+)$", RegexOption.IGNORE_CASE)
private val LOCALHOST_EMULATOR_PATTERN = Regex("^(?:127\\.0\\.0\\.1|localhost):(\\d+)$", RegexOption.IGNORE_CASE)
private val TASK_QUEUE_ENTRYPOINT_NAMES = setOf("agent-task-queue", "task_queue", "task_queue.py")

data class QueueConfigurationSnapshot(
    val serverProcesses: List<QueueServerProcess>,
    val configuredScopes: List<ConfiguredQueueScope>,
    val statusMessage: String? = null,
    val errorMessage: String? = null,
) {
    val serverCount: Int = serverProcesses.size
    val totalSlots: Int = configuredScopes.sumOf { it.capacity ?: 0 }
    val configuredEmulatorScopeCount: Int = configuredScopes.count { it.isEmulatorLike }

    companion object {
        val EMPTY = QueueConfigurationSnapshot(
            serverProcesses = emptyList(),
            configuredScopes = emptyList(),
        )
    }
}

data class QueueServerProcess(
    val pid: Int,
    val parentPid: Int? = null,
    val commandLine: String,
    val dataDir: Path,
    val queueCapacities: Map<String, Int>,
    val agentLabel: String = "Task Queue",
    val contextLabel: String? = null,
)

data class ConfiguredQueueScope(
    val scopeName: String,
    val capacities: Set<Int>,
    val sourcePids: List<Int>,
) {
    val capacity: Int? = capacities.singleOrNull()
    val hasConflict: Boolean = capacities.size > 1
    val rootScope: String = scopeName.substringBefore('/')
    val leafName: String = scopeName.substringAfterLast('/')
    val emulatorPort: String? = extractEmulatorPort(leafName)
    val isEmulatorLike: Boolean = emulatorPort != null

    val displayCapacityLabel: String = when {
        capacity != null -> capacity.toString()
        else -> capacities.sorted().joinToString(" / ")
    }
}

data class AdbSnapshot(
    val devices: List<AdbDevice>,
    val statusMessage: String? = null,
    val errorMessage: String? = null,
) {
    val connectedDevices: Int = devices.count { it.isConnected }
    val connectedEmulators: Int = devices.count { it.isConnected && it.isEmulator }

    companion object {
        val EMPTY = AdbSnapshot(devices = emptyList())
    }
}

data class AdbDevice(
    val serial: String,
    val state: String,
    val details: Map<String, String>,
) {
    val isConnected: Boolean = state.equals("device", ignoreCase = true)
    val emulatorPort: String? = extractEmulatorPort(serial)
    val isEmulator: Boolean = emulatorPort != null ||
        details["device"]?.contains("emu", ignoreCase = true) == true ||
        details["model"]?.contains("sdk", ignoreCase = true) == true

    val detailLine: String = buildList {
        if (!isConnected) add(state)
        details["model"]?.takeIf { it.isNotBlank() }?.let(::add)
        details["device"]?.takeIf { it.isNotBlank() }?.let(::add)
        details["transport_id"]?.takeIf { it.isNotBlank() }?.let { add("transport $it") }
    }.joinToString(" · ")
}

object TaskQueueProcessInspector {
    fun loadConfiguration(dataDir: Path): QueueConfigurationSnapshot {
        val normalizedDataDir = dataDir.toAbsolutePath().normalize()
        val commandResult = runCommandCandidates(
            listOf(
                listOf("ps", "eww", "-axo", "pid=,ppid=,command="),
                listOf("ps", "eww", "axo", "pid=,ppid=,command="),
            )
        )

        if (commandResult.errorMessage != null) {
            return QueueConfigurationSnapshot(
                serverProcesses = emptyList(),
                configuredScopes = emptyList(),
                errorMessage = commandResult.errorMessage,
            )
        }

        if (commandResult.exitCode != 0) {
            return QueueConfigurationSnapshot(
                serverProcesses = emptyList(),
                configuredScopes = emptyList(),
                errorMessage = commandResult.output.ifBlank { "Failed to inspect task queue processes." },
            )
        }

        val serverProcesses = parseTaskQueueProcesses(commandResult.output)
            .filter { it.dataDir == normalizedDataDir }

        if (serverProcesses.isEmpty()) {
            return QueueConfigurationSnapshot(
                serverProcesses = emptyList(),
                configuredScopes = emptyList(),
                statusMessage = "No live task queue server detected for $normalizedDataDir. Exact queues default to capacity 1 unless a matching server is running.",
            )
        }

        val scopesByName = linkedMapOf<String, MutableSet<Int>>()
        val scopePids = linkedMapOf<String, MutableSet<Int>>()
        serverProcesses.forEach { process ->
            process.queueCapacities.forEach { (scopeName, capacity) ->
                scopesByName.getOrPut(scopeName) { linkedSetOf() }.add(capacity)
                scopePids.getOrPut(scopeName) { linkedSetOf() }.add(process.pid)
            }
        }

        val configuredScopes = scopesByName.entries
            .sortedBy { it.key }
            .map { (scopeName, capacities) ->
                ConfiguredQueueScope(
                    scopeName = scopeName,
                    capacities = capacities.toSortedSet(),
                    sourcePids = scopePids[scopeName].orEmpty().sorted(),
                )
            }

        val conflictingScopes = configuredScopes.filter { it.hasConflict }
        val statusMessage = when {
            configuredScopes.isEmpty() -> "Detected ${serverProcesses.size} live task queue server(s), but none advertise --queue-capacity overrides."
            conflictingScopes.isEmpty() -> "Detected ${serverProcesses.size} live task queue server(s) with ${configuredScopes.size} configured scope(s) for this data dir."
            else -> null
        }
        val errorMessage = conflictingScopes
            .takeIf { it.isNotEmpty() }
            ?.joinToString(
                prefix = "Conflicting queue-capacity values detected for: ",
                separator = ", ",
            ) { "${it.scopeName} (${it.displayCapacityLabel})" }

        return QueueConfigurationSnapshot(
            serverProcesses = serverProcesses,
            configuredScopes = configuredScopes,
            statusMessage = statusMessage,
            errorMessage = errorMessage,
        )
    }
}

object AdbInspector {
    fun loadSnapshot(): AdbSnapshot {
        val commandResult = runCommand("adb", "devices", "-l")
        if (commandResult.errorMessage != null) {
            return AdbSnapshot(
                devices = emptyList(),
                statusMessage = commandResult.errorMessage,
            )
        }

        if (commandResult.exitCode != 0) {
            return AdbSnapshot(
                devices = emptyList(),
                errorMessage = commandResult.output.ifBlank { "`adb devices -l` failed." },
            )
        }

        return parseAdbSnapshot(commandResult.output)
    }
}

internal data class CommandResult(
    val output: String,
    val exitCode: Int,
    val errorMessage: String? = null,
)

internal fun parseTaskQueueProcesses(output: String): List<QueueServerProcess> {
    val processEntries = output.lineSequence()
        .mapNotNull(::parseProcessLine)
        .toList()
    val processesByPid = processEntries.associateBy { it.pid }

    return processEntries
        .filter { looksLikeTaskQueueServer(it.tokens) }
        .map { processEntry ->
            val dataDir = resolveTaskQueueDataDir(processEntry.tokens)
            QueueServerProcess(
                pid = processEntry.pid,
                parentPid = processEntry.parentPid,
                commandLine = processEntry.commandLine,
                dataDir = dataDir,
                queueCapacities = parseQueueCapacities(processEntry.tokens),
                agentLabel = inferProcessAgentLabel(processEntry, processesByPid),
                contextLabel = inferProcessContextLabel(processEntry, processesByPid),
            )
        }
        .sortedBy { it.pid }
        .toList()
}

internal fun parseAdbSnapshot(output: String): AdbSnapshot {
    val cleanedLines = output.lineSequence()
        .map { it.trim() }
        .filter { it.isNotBlank() }
        .filterNot { it.startsWith("*") }
        .toList()

    val headerIndex = cleanedLines.indexOfFirst { it.startsWith("List of devices attached") }
    if (headerIndex == -1) {
        return AdbSnapshot(
            devices = emptyList(),
            errorMessage = output.ifBlank { "Unexpected output from `adb devices -l`." },
        )
    }

    val devices = cleanedLines.drop(headerIndex + 1)
        .mapNotNull(::parseAdbDevice)
        .sortedWith(compareBy<AdbDevice>({ !it.isConnected }, { it.serial }))

    return AdbSnapshot(
        devices = devices,
        statusMessage = if (devices.isEmpty()) "No ADB devices detected." else null,
    )
}

internal fun extractEmulatorPort(value: String): String? {
    val trimmed = value.trim()
    EMULATOR_SERIAL_PATTERN.matchEntire(trimmed)?.let {
        return it.groupValues[1]
    }
    LOCALHOST_EMULATOR_PATTERN.matchEntire(trimmed)?.let {
        return it.groupValues[1]
    }
    return null
}

private data class ProcessEntry(
    val pid: Int,
    val parentPid: Int?,
    val tokens: List<String>,
    val commandLine: String,
)

private fun parseProcessLine(line: String): ProcessEntry? {
    val trimmed = line.trim()
    if (trimmed.isEmpty()) return null

    val firstSpace = trimmed.indexOfFirst { it.isWhitespace() }
    if (firstSpace <= 0) return null

    val pid = trimmed.substring(0, firstSpace).toIntOrNull() ?: return null
    val remainder = trimmed.substring(firstSpace).trimStart()
    if (remainder.isEmpty()) return null

    val secondSpace = remainder.indexOfFirst { it.isWhitespace() }
    val secondToken = if (secondSpace > 0) remainder.substring(0, secondSpace) else null
    val parentPid = secondToken?.toIntOrNull()
    val commandLine = if (parentPid != null && secondSpace > 0) {
        remainder.substring(secondSpace).trim()
    } else {
        remainder.trim()
    }
    if (commandLine.isEmpty()) return null

    return ProcessEntry(
        pid = pid,
        parentPid = parentPid,
        tokens = shellSplit(commandLine),
        commandLine = commandLine,
    )
}

private fun looksLikeTaskQueueServer(tokens: List<String>): Boolean {
    if (tokens.isEmpty()) return false

    val executable = tokens.first().substringAfterLast('/')
    if (executable.startsWith("python")) {
        return firstPythonEntrypointToken(tokens.drop(1))
            ?.let(::looksLikeTaskQueueEntrypoint)
            ?: false
    }

    return tokens.first().let(::looksLikeTaskQueueEntrypoint)
}

private fun looksLikeTaskQueueEntrypoint(token: String): Boolean {
    return token.substringAfterLast('/') in TASK_QUEUE_ENTRYPOINT_NAMES
}

private fun firstPythonEntrypointToken(tokens: List<String>): String? {
    var index = 0
    while (index < tokens.size) {
        val token = tokens[index]
        when {
            token == "-m" -> return tokens.getOrNull(index + 1)
            token == "-c" -> return null
            token == "-W" || token == "-X" -> index += 2
            token.startsWith('-') -> index += 1
            looksLikeEnvironmentAssignment(token) -> index += 1
            else -> return token
        }
    }
    return null
}

private fun looksLikeEnvironmentAssignment(token: String): Boolean {
    val separator = token.indexOf('=')
    if (separator <= 0) return false

    val name = token.substring(0, separator)
    val startsLikeEnvName = name.firstOrNull()?.let { it == '_' || it.isLetter() } == true
    return startsLikeEnvName && name.all { it == '_' || it.isLetterOrDigit() }
}

private fun inferProcessAgentLabel(
    processEntry: ProcessEntry,
    processesByPid: Map<Int, ProcessEntry>,
): String {
    return (listOf(processEntry) + ancestorChain(processEntry, processesByPid))
        .mapNotNull(::knownAgentLabel)
        .firstOrNull()
        ?: "Task Queue"
}

private fun inferProcessContextLabel(
    processEntry: ProcessEntry,
    processesByPid: Map<Int, ProcessEntry>,
): String? {
    return (listOf(processEntry) + ancestorChain(processEntry, processesByPid))
        .mapNotNull { entry -> findProcessDirectory(entry.tokens) }
        .map(::compactPathLabel)
        .firstOrNull()
}

private fun ancestorChain(
    processEntry: ProcessEntry,
    processesByPid: Map<Int, ProcessEntry>,
): List<ProcessEntry> {
    val ancestors = mutableListOf<ProcessEntry>()
    val visited = mutableSetOf<Int>()
    var currentPid = processEntry.parentPid
    while (currentPid != null && visited.add(currentPid)) {
        val ancestor = processesByPid[currentPid] ?: break
        ancestors += ancestor
        currentPid = ancestor.parentPid
    }
    return ancestors
}

private fun knownAgentLabel(processEntry: ProcessEntry): String? {
    val executable = processEntry.tokens.firstOrNull()?.substringAfterLast('/') ?: return null
    return when {
        executable == "amp" -> buildAmpLabel(processEntry.tokens)
        executable.startsWith("claude") -> "Claude"
        executable.startsWith("codex") -> "Codex"
        executable.startsWith("cursor") -> "Cursor"
        executable == "zed" -> "Zed"
        executable == "windsurf" -> "Windsurf"
        else -> null
    }
}

private fun buildAmpLabel(tokens: List<String>): String {
    val mode = tokens.zipWithNext().firstOrNull { (current, _) ->
        current == "-m" || current == "--mode"
    }?.second ?: tokens.firstOrNull { it.startsWith("--mode=") }?.substringAfter('=')

    return if (mode.isNullOrBlank()) {
        "Amp"
    } else {
        "Amp ${mode.trim()}"
    }
}

private fun findProcessDirectory(tokens: List<String>): String? {
    var index = 0
    while (index < tokens.size) {
        val token = tokens[index]
        when {
            token.startsWith("--directory=") -> return token.substringAfter('=')
            token == "--directory" && index + 1 < tokens.size -> return tokens[index + 1]
            token.startsWith("--cwd=") -> return token.substringAfter('=')
            token == "--cwd" && index + 1 < tokens.size -> return tokens[index + 1]
        }
        index += 1
    }
    return null
}

private fun resolveTaskQueueDataDir(tokens: List<String>): Path {
    var index = 0
    while (index < tokens.size) {
        val token = tokens[index]
        when {
            token.startsWith("--data-dir=") -> {
                return Paths.get(token.substringAfter('='))
                    .toAbsolutePath()
                    .normalize()
            }
            token == "--data-dir" && index + 1 < tokens.size -> {
                return Paths.get(tokens[index + 1])
                    .toAbsolutePath()
                    .normalize()
            }
        }
        index += 1
    }

    tokens.firstOrNull { it.startsWith("TASK_QUEUE_DATA_DIR=") }
        ?.substringAfter('=')
        ?.takeIf { it.isNotBlank() }
        ?.let { configuredPath ->
            return Paths.get(configuredPath)
                .toAbsolutePath()
                .normalize()
        }

    return DEFAULT_QUEUE_DATA_DIR
}

private fun parseQueueCapacities(tokens: List<String>): Map<String, Int> {
    val capacities = linkedMapOf<String, Int>()
    var index = 0
    while (index < tokens.size) {
        val token = tokens[index]
        val rawSpec = when {
            token.startsWith("--queue-capacity=") -> token.substringAfter("--queue-capacity=")
            token == "--queue-capacity" && index + 1 < tokens.size -> {
                index += 1
                tokens[index]
            }
            else -> null
        }

        if (rawSpec != null) {
            val separator = rawSpec.indexOf('=')
            if (separator > 0 && separator < rawSpec.lastIndex) {
                val scopeName = rawSpec.substring(0, separator)
                val capacity = rawSpec.substring(separator + 1).toIntOrNull()
                if (capacity != null) {
                    capacities[scopeName] = capacity
                }
            }
        }

        index += 1
    }

    return capacities
}

private fun parseAdbDevice(line: String): AdbDevice? {
    val parts = line.split(Regex("\\s+"))
    if (parts.size < 2) return null

    val details = parts.drop(2)
        .mapNotNull { segment ->
            val separator = segment.indexOf(':')
            if (separator <= 0) {
                null
            } else {
                segment.substring(0, separator) to segment.substring(separator + 1)
            }
        }
        .toMap()

    return AdbDevice(
        serial = parts[0],
        state = parts[1],
        details = details,
    )
}

internal fun runCommand(vararg command: String): CommandResult {
    val outputReader = Executors.newSingleThreadExecutor()
    return try {
        val process = ProcessBuilder(*command)
            .redirectErrorStream(true)
            .start()
        val outputFuture = outputReader.submit<String> {
            process.inputStream.bufferedReader().use { it.readText() }
        }
        if (!process.waitFor(COMMAND_TIMEOUT_SECONDS, TimeUnit.SECONDS)) {
            process.destroyForcibly()
            process.waitFor()
            outputFuture.cancel(true)
            return CommandResult(
                output = "",
                exitCode = -1,
                errorMessage = "`${command.joinToString(" ")}` timed out after ${COMMAND_TIMEOUT_SECONDS}s.",
            )
        }

        val output = outputFuture.get()
        CommandResult(output = output.trim(), exitCode = process.exitValue())
    } catch (error: Exception) {
        CommandResult(
            output = "",
            exitCode = -1,
            errorMessage = error.message ?: "Failed to run `${command.joinToString(" ")}`.",
        )
    } finally {
        outputReader.shutdownNow()
    }
}

internal fun runCommandCandidates(
    candidates: List<List<String>>,
    runner: (List<String>) -> CommandResult = { runCommand(*it.toTypedArray()) },
): CommandResult {
    var lastResult = CommandResult(output = "", exitCode = -1, errorMessage = "No command candidates provided.")
    candidates.forEach { command ->
        val result = runner(command)
        if (result.errorMessage == null && result.exitCode == 0) {
            return result
        }
        lastResult = result
    }
    return lastResult
}

private fun shellSplit(commandLine: String): List<String> {
    val tokens = mutableListOf<String>()
    val current = StringBuilder()
    var quote: Char? = null
    var escaping = false

    fun flush() {
        if (current.isNotEmpty()) {
            tokens += current.toString()
            current.clear()
        }
    }

    commandLine.forEach { ch ->
        when {
            escaping -> {
                current.append(ch)
                escaping = false
            }
            ch == '\\' && quote != '\'' -> escaping = true
            quote != null && ch == quote -> quote = null
            quote == null && (ch == '"' || ch == '\'') -> quote = ch
            quote == null && ch.isWhitespace() -> flush()
            else -> current.append(ch)
        }
    }
    flush()

    return tokens
}
