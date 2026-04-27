package com.block.agenttaskqueue.sidecar

import java.nio.file.Path
import java.nio.file.Paths
import java.time.Duration
import java.time.Instant
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.ZoneOffset

data class QueueTask(
    val id: Int,
    val queueName: String,
    val status: String,
    val command: String?,
    val pid: Int?,
    val childPid: Int?,
    val createdAt: String?,
    val updatedAt: String?,
    val workingDirectory: String? = null,
    val worktreeRoot: String? = null,
    val repoName: String? = null,
    val gitBranch: String? = null,
    val agentName: String? = null,
) {
    val displayCommand: String
        get() = (command ?: "unknown").replace(Regex("^(\\w+=\\S+\\s+)+"), "")

    val displayAgentLabel: String?
        get() = agentName
            ?.trim()
            ?.takeIf { it.isNotBlank() }
            ?.let(::normalizeAgentLabel)

    val displayContextLabel: String?
        get() = buildList {
            repoName?.trim()?.takeIf { it.isNotBlank() }?.let(::add)
            gitBranch?.trim()?.takeIf { it.isNotBlank() }?.let(::add)

            if (gitBranch.isNullOrBlank()) {
                worktreeRoot?.trim()?.takeIf { it.isNotBlank() }?.let(::compactPathLabel)?.let(::add)
            }
            if (isEmpty()) {
                workingDirectory?.trim()?.takeIf { it.isNotBlank() }?.let(::compactPathLabel)?.let(::add)
            }
        }
            .distinct()
            .joinToString(" · ")
            .takeIf { it.isNotBlank() }

    val displayIdentityLabel: String?
        get() = listOfNotNull(displayAgentLabel, displayContextLabel)
            .joinToString(" · ")
            .takeIf { it.isNotBlank() }

    fun statusAge(now: Instant = Instant.now()): String {
        val reference = when (status.lowercase()) {
            "running" -> parseQueueInstant(updatedAt, ZoneId.systemDefault()) ?: parseQueueInstant(createdAt)
            else -> parseQueueInstant(createdAt)
        } ?: return "time unknown"

        val prefix = if (status.equals("running", ignoreCase = true)) "running" else "queued"
        return "$prefix ${relativeDuration(now, reference)}"
    }
}

data class QueueSummary(
    val total: Int,
    val running: Int,
    val waiting: Int,
) {
    companion object {
        fun fromTasks(tasks: List<QueueTask>): QueueSummary {
            return QueueSummary(
                total = tasks.size,
                running = tasks.count { it.status.equals("running", ignoreCase = true) },
                waiting = tasks.count { it.status.equals("waiting", ignoreCase = true) },
            )
        }
    }
}

data class QueueLane(
    val queueName: String,
    val tasks: List<QueueTask>,
    val configuredScope: ConfiguredQueueScope? = null,
) {
    val runningCount: Int = tasks.count { it.status.equals("running", ignoreCase = true) }
    val waitingCount: Int = tasks.count { it.status.equals("waiting", ignoreCase = true) }
    val configuredCapacity: Int? = configuredScope?.capacity
    val hasCapacityConflict: Boolean = configuredScope?.hasConflict == true
    val exactCapacity: Int = configuredCapacity ?: 1
    val emulatorPort: String? = extractEmulatorPort(queueName.substringAfterLast('/'))
    val isEmulatorLike: Boolean = emulatorPort != null
}

data class ScopeGroup(
    val scopeName: String,
    val lanes: List<QueueLane>,
) {
    val taskCount: Int = lanes.sumOf { it.tasks.size }
    val runningCount: Int = lanes.sumOf { it.runningCount }
    val waitingCount: Int = lanes.sumOf { it.waitingCount }
}

data class ConfiguredScopeUsage(
    val configuredScope: ConfiguredQueueScope,
    val runningCount: Int,
    val waitingCount: Int,
    val descendantQueueCount: Int,
    val sourceServerLabels: List<String> = emptyList(),
) {
    val scopeName: String = configuredScope.scopeName
    val capacity: Int? = configuredScope.capacity
    val displayCapacityLabel: String = configuredScope.displayCapacityLabel
    val usedSlots: Int = capacity?.let { runningCount.coerceAtMost(it) } ?: runningCount
}

data class ServerIdentity(
    val primaryLabel: String,
    val contextLabel: String? = null,
    val launchContextLabel: String? = null,
    val detailLabel: String? = null,
) {
    val displayLabel: String = listOfNotNull(primaryLabel, contextLabel)
        .joinToString(" · ")
        .takeIf { it.isNotBlank() }
        ?: primaryLabel
}

data class EmulatorAlignment(
    val configuredQueues: List<QueueLane>,
    val connectedDevices: List<AdbDevice>,
    val matchedPorts: Set<String>,
) {
    val unmatchedConfiguredQueues: List<QueueLane> = configuredQueues.filter { lane ->
        lane.emulatorPort == null || lane.emulatorPort !in matchedPorts
    }
    val unmatchedDevices: List<AdbDevice> = connectedDevices.filter { device ->
        device.emulatorPort == null || device.emulatorPort !in matchedPorts
    }
}

data class QueueSnapshot(
    val dataDir: Path,
    val tasks: List<QueueTask>,
    val refreshedAt: Instant,
    val configuration: QueueConfigurationSnapshot = QueueConfigurationSnapshot.EMPTY,
    val adb: AdbSnapshot = AdbSnapshot.EMPTY,
    val metrics: QueueMetricsSnapshot = QueueMetricsSnapshot.EMPTY,
    val statusMessage: String? = null,
    val errorMessage: String? = null,
) {
    val summary: QueueSummary = QueueSummary.fromTasks(tasks)
    val runningTasks: List<QueueTask> = tasks.filter { it.status.equals("running", ignoreCase = true) }
    val waitingTasks: List<QueueTask> = tasks.filter { it.status.equals("waiting", ignoreCase = true) }
    val queueLanes: List<QueueLane> = buildQueueLanes(tasks, configuration)
    private val tasksByServerPid: Map<Int, List<QueueTask>> = tasks
        .mapNotNull { task -> task.pid?.let { pid -> pid to task } }
        .groupBy(keySelector = { it.first }, valueTransform = { it.second })
    val serverIdentityByPid: Map<Int, ServerIdentity> = configuration.serverProcesses.associate { process ->
        process.pid to buildServerIdentity(
            process = process,
            tasks = tasksByServerPid[process.pid].orEmpty(),
            historicalUsage = metrics.latestUsageByPid[process.pid],
        )
    }
    val scopeGroups: List<ScopeGroup> = queueLanes
        .groupBy { rootScope(it.queueName) }
        .toSortedMap()
        .map { (scopeName, lanes) -> ScopeGroup(scopeName, lanes) }
    val configuredScopeUsage: List<ConfiguredScopeUsage> = configuration.configuredScopes
        .map { configuredScope ->
            ConfiguredScopeUsage(
                configuredScope = configuredScope,
                runningCount = tasks.count { task ->
                    task.status.equals("running", ignoreCase = true) && inScope(task.queueName, configuredScope.scopeName)
                },
                waitingCount = tasks.count { task ->
                    task.status.equals("waiting", ignoreCase = true) && inScope(task.queueName, configuredScope.scopeName)
                },
                descendantQueueCount = queueLanes.count { lane -> inScope(lane.queueName, configuredScope.scopeName) },
                sourceServerLabels = configuredScope.sourcePids.map { sourcePid ->
                    serverIdentityByPid[sourcePid]?.displayLabel ?: "pid $sourcePid"
                },
            )
        }
        .sortedBy { it.scopeName }
    val emulatorAlignment: EmulatorAlignment = buildEmulatorAlignment(queueLanes, adb)

    companion object {
        fun empty(
            dataDir: Path,
            configuration: QueueConfigurationSnapshot = QueueConfigurationSnapshot.EMPTY,
            adb: AdbSnapshot = AdbSnapshot.EMPTY,
            metrics: QueueMetricsSnapshot = QueueMetricsSnapshot.EMPTY,
            statusMessage: String? = null,
            errorMessage: String? = null,
        ): QueueSnapshot {
            return QueueSnapshot(
                dataDir = dataDir,
                tasks = emptyList(),
                refreshedAt = Instant.now(),
                configuration = configuration,
                adb = adb,
                metrics = metrics,
                statusMessage = statusMessage,
                errorMessage = errorMessage,
            )
        }

        fun fromTasks(
            dataDir: Path,
            tasks: List<QueueTask>,
            configuration: QueueConfigurationSnapshot = QueueConfigurationSnapshot.EMPTY,
            adb: AdbSnapshot = AdbSnapshot.EMPTY,
            metrics: QueueMetricsSnapshot = QueueMetricsSnapshot.EMPTY,
            statusMessage: String? = null,
        ): QueueSnapshot {
            return QueueSnapshot(
                dataDir = dataDir,
                tasks = tasks.sortedWith(compareBy<QueueTask>({ it.queueName }, { it.id })),
                refreshedAt = Instant.now(),
                configuration = configuration,
                adb = adb,
                metrics = metrics,
                statusMessage = statusMessage,
            )
        }
    }
}

fun parseQueueInstant(raw: String?, defaultZone: ZoneId = ZoneOffset.UTC): Instant? {
    if (raw.isNullOrBlank()) {
        return null
    }

    val normalized = raw.replace(" ", "T")
    return runCatching {
        LocalDateTime.parse(normalized).atZone(defaultZone).toInstant()
    }.getOrNull()
}

fun formatRefreshTime(instant: Instant): String {
    val localTime = instant.atZone(java.time.ZoneId.systemDefault()).toLocalTime()
    return localTime.truncatedTo(java.time.temporal.ChronoUnit.SECONDS).toString()
}

internal fun normalizeAgentLabel(raw: String): String {
    return when (raw.trim().lowercase()) {
        "amp" -> "Amp"
        "claude" -> "Claude"
        "codex" -> "Codex"
        "cursor" -> "Cursor"
        else -> raw.trim()
    }
}

internal fun compactPathLabel(raw: String): String {
    return runCatching {
        Paths.get(raw).normalize().fileName?.toString() ?: raw
    }.getOrElse { raw }
}

private fun rootScope(queueName: String): String = queueName.substringBefore('/')

private fun buildServerIdentity(
    process: QueueServerProcess,
    tasks: List<QueueTask>,
    historicalUsage: HistoricalTaskUsage?,
): ServerIdentity {
    val agentLabels = tasks.mapNotNull { it.displayAgentLabel }.distinct()
    val contextLabels = tasks.mapNotNull { it.displayContextLabel }.distinct()
    val hasVisibleUsage = tasks.isNotEmpty()
    return ServerIdentity(
        primaryLabel = summarizeIdentityLabels(agentLabels)
            ?: historicalUsage?.displayAgentLabel
            ?: process.agentLabel,
        contextLabel = summarizeIdentityLabels(contextLabels)
            ?: historicalUsage?.displayContextLabel,
        launchContextLabel = process.contextLabel,
        detailLabel = if (hasVisibleUsage) {
            "${tasks.size} visible task${if (tasks.size == 1) "" else "s"}"
        } else {
            "idle server"
        },
    )
}

private fun summarizeIdentityLabels(labels: List<String>): String? {
    return when {
        labels.isEmpty() -> null
        labels.size <= 2 -> labels.joinToString(" + ")
        else -> "${labels.first()} +${labels.size - 1} more"
    }
}

private fun buildQueueLanes(
    tasks: List<QueueTask>,
    configuration: QueueConfigurationSnapshot,
): List<QueueLane> {
    val tasksByQueue = tasks.groupBy { it.queueName }
    val configuredScopes = configuration.configuredScopes.associateBy { it.scopeName }
    return (tasksByQueue.keys + configuredScopes.keys)
        .toSortedSet()
        .map { queueName ->
            QueueLane(
                queueName = queueName,
                tasks = tasksByQueue[queueName].orEmpty().sortedBy { it.id },
                configuredScope = configuredScopes[queueName],
            )
        }
}

private fun buildEmulatorAlignment(
    queueLanes: List<QueueLane>,
    adb: AdbSnapshot,
): EmulatorAlignment {
    val configuredQueues = queueLanes.filter { it.configuredScope?.isEmulatorLike == true }
    val connectedDevices = adb.devices.filter { it.isConnected && it.isEmulator }
    val matchedPorts = configuredQueues.mapNotNull { it.emulatorPort }.toSet()
        .intersect(connectedDevices.mapNotNull { it.emulatorPort }.toSet())
    return EmulatorAlignment(
        configuredQueues = configuredQueues,
        connectedDevices = connectedDevices,
        matchedPorts = matchedPorts,
    )
}

private fun inScope(queueName: String, scopeName: String): Boolean {
    return queueName == scopeName || queueName.startsWith("$scopeName/")
}

private fun relativeDuration(now: Instant, then: Instant): String {
    val elapsed = Duration.between(then, now).seconds.coerceAtLeast(0)
    return when {
        elapsed < 60 -> "${elapsed}s"
        elapsed < 3600 -> "${elapsed / 60}m"
        elapsed < 86_400 -> "${elapsed / 3600}h ${elapsed % 3600 / 60}m"
        else -> "${elapsed / 86_400}d ${elapsed % 86_400 / 3600}h"
    }
}
