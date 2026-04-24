package com.block.agenttaskqueue.sidecar

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.TooltipArea
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Window
import androidx.compose.ui.window.application
import androidx.compose.ui.window.rememberWindowState
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.nio.file.Path
import java.nio.file.Paths
import kotlin.system.exitProcess

private const val ACTIVE_INTERVAL_MS = 1000L
private const val IDLE_INTERVAL_MS = 3000L

private val ScopeAccent = Color(0xFF305B78)
private val RunningAccent = Color(0xFFC96A3D)
private val WaitingAccent = Color(0xFF3F7698)
private val LaneAccent = Color(0xFF46705C)
private val WarningAccent = Color(0xFFB35C33)
private val ScopeCardColor = Color(0xFFFFFBF6)
private val LaneCardColor = Color(0xFFFFFDF9)
private val TooltipColor = Color(0xFF2B2F35)

private val DashboardColors = lightColorScheme(
    primary = Color(0xFF305B78),
    secondary = Color(0xFFB35C33),
    tertiary = Color(0xFF46705C),
    background = Color(0xFFF7F1E8),
    surface = Color(0xFFFFFCF8),
    surfaceVariant = Color(0xFFE9DFCf),
    onBackground = Color(0xFF1F262D),
    onSurface = Color(0xFF1F262D),
    outline = Color(0xFF877F74),
    error = Color(0xFF8D2C2C),
)

fun main(args: Array<String>) = application {
    val dataDir = resolveDataDir(args)

    Window(
        onCloseRequest = ::exitApplication,
        title = "Agent Task Queue Sidecar",
        state = rememberWindowState(width = 1320.dp, height = 900.dp),
    ) {
        MaterialTheme(colorScheme = DashboardColors) {
            QueueDashboard(dataDir = dataDir)
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun QueueDashboard(dataDir: Path) {
    val refreshRequests = remember(dataDir) { Channel<Unit>(Channel.CONFLATED) }
    var snapshot by remember(dataDir) {
        mutableStateOf(QueueSnapshot.empty(dataDir, statusMessage = "Loading queue state..."))
    }

    LaunchedEffect(dataDir) {
        while (true) {
            snapshot = withContext(Dispatchers.IO) { TaskQueueDatabase.loadSnapshot(dataDir) }
            val interval = if (snapshot.tasks.isNotEmpty()) ACTIVE_INTERVAL_MS else IDLE_INTERVAL_MS
            withTimeoutOrNull(interval) {
                refreshRequests.receive()
            }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                        Text("Agent Task Queue", fontWeight = FontWeight.SemiBold)
                        Text(
                            text = snapshot.dataDir.toString(),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.7f),
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                },
                actions = {
                    Text(
                        text = "Updated ${formatRefreshTime(snapshot.refreshedAt)}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.7f),
                    )
                    Spacer(Modifier.width(12.dp))
                    Button(onClick = { refreshRequests.trySend(Unit) }) {
                        Text("Refresh")
                    }
                    Spacer(Modifier.width(16.dp))
                },
            )
        },
    ) { innerPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .background(
                    Brush.verticalGradient(
                        listOf(
                            MaterialTheme.colorScheme.background,
                            Color(0xFFF3ECE2),
                        )
                    )
                )
                .verticalScroll(rememberScrollState())
                .padding(innerPadding)
                .padding(horizontal = 22.dp, vertical = 16.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            snapshot.errorMessage?.let { ErrorBanner(it) }
            snapshot.configuration.errorMessage?.let { ErrorBanner(it) }
            snapshot.adb.errorMessage?.let { ErrorBanner(it) }
            QueueFlowSection(snapshot)
            EnvironmentDetailsSection(snapshot)

            Text(
                text = "Scopes own shared slots. Exact queues stay FIFO. Inline chips keep run vs wait local, while diagnostics below show which agent context owns each live server.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.65f),
            )
        }
    }
}

@Composable
private fun QueueFlowSection(snapshot: QueueSnapshot) {
    SectionCard(
        title = "Queue Flow",
        subtitle = "Scopes own shared capacity; exact queues keep FIFO order inside each lane.",
    ) {
        if (snapshot.scopeGroups.isEmpty()) {
            Text(
                text = snapshot.statusMessage ?: "No queues are visible yet.",
                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.65f),
            )
            return@SectionCard
        }

        Column(verticalArrangement = Arrangement.spacedBy(14.dp)) {
            snapshot.scopeGroups.forEach { scope ->
                ScopeFlowCard(scope = scope, snapshot = snapshot)
            }
        }
    }
}

@Composable
private fun ScopeFlowCard(
    scope: ScopeGroup,
    snapshot: QueueSnapshot,
) {
    val rootUsage = snapshot.configuredScopeUsage.firstOrNull { it.scopeName == scope.scopeName }
    val emulatorLanes = scope.lanes.filter { it.isEmulatorLike || it.configuredScope?.isEmulatorLike == true }
    val matchedEmulators = emulatorLanes.count { lane -> lane.emulatorPort != null && lane.emulatorPort in snapshot.emulatorAlignment.matchedPorts }

    Card(colors = CardDefaults.cardColors(containerColor = ScopeCardColor)) {
        Column(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 14.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.Top,
            ) {
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(scope.scopeName, style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.SemiBold)
                    Text(
                        text = "${scope.lanes.size} lane(s) · ${scope.runningCount} running · ${scope.waitingCount} waiting",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.72f),
                    )
                    Row(
                        modifier = Modifier.horizontalScroll(rememberScrollState()),
                        horizontalArrangement = Arrangement.spacedBy(6.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        MetaBadge(
                            label = rootUsage?.let { "shared ${it.displayCapacityLabel}" } ?: "default per-lane",
                            accent = ScopeAccent,
                            filled = true,
                        )
                        val configuredDescendants = snapshot.configuredScopeUsage.count {
                            it.scopeName != scope.scopeName && it.scopeName.startsWith("${scope.scopeName}/")
                        }
                        if (configuredDescendants > 0) {
                            MetaBadge(
                                label = "$configuredDescendants configured descendant lane(s)",
                                accent = LaneAccent,
                            )
                        }
                        if (emulatorLanes.isNotEmpty()) {
                            MetaBadge(
                                label = "ADB $matchedEmulators/${emulatorLanes.size} matched",
                                accent = WarningAccent,
                                tooltip = "Matches configured emulator queue lanes against connected `adb devices -l` emulator serials. A lane is matched when both advertise the same emulator port.",
                            )
                        }
                    }
                }

                Column(horizontalAlignment = Alignment.End, verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    SlotStrip(
                        capacity = rootUsage?.capacity,
                        usedSlots = rootUsage?.usedSlots ?: scope.runningCount,
                        accent = ScopeAccent,
                        fallbackLabel = if (rootUsage == null) "No shared cap configured" else null,
                    )
                }
            }

            HorizontalDivider(color = MaterialTheme.colorScheme.outline.copy(alpha = 0.28f))

            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                scope.lanes
                    .sortedWith(
                        compareByDescending<QueueLane> { it.runningCount > 0 }
                            .thenByDescending { it.waitingCount > 0 }
                            .thenByDescending { it.configuredScope != null }
                            .thenBy { it.queueName }
                    )
                    .forEach { lane ->
                        LaneFlowRow(
                            lane = lane,
                            rootUsage = rootUsage,
                            emulatorMatched = lane.emulatorPort != null && lane.emulatorPort in snapshot.emulatorAlignment.matchedPorts,
                        )
                    }
            }
        }
    }
}

@Composable
private fun LaneFlowRow(
    lane: QueueLane,
    rootUsage: ConfiguredScopeUsage?,
    emulatorMatched: Boolean,
) {
    val runningTasks = lane.tasks.filter { it.status.equals("running", ignoreCase = true) }
    val waitingTasks = lane.tasks.filter { it.status.equals("waiting", ignoreCase = true) }
    val laneLabel = lane.queueName.substringAfterLast('/')
    val fullPathNeeded = laneLabel != lane.queueName

    Surface(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(16.dp),
        color = LaneCardColor,
        tonalElevation = 1.dp,
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .border(1.dp, MaterialTheme.colorScheme.outline.copy(alpha = 0.14f), RoundedCornerShape(16.dp))
                .padding(horizontal = 12.dp, vertical = 10.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.Top,
            ) {
                Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    Text(laneLabel, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold)
                    if (fullPathNeeded) {
                        Text(
                            text = lane.queueName,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.62f),
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                }

                Text(
                    text = "${runningTasks.size} running · ${waitingTasks.size} waiting",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.7f),
                )
            }

            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .horizontalScroll(rememberScrollState()),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                MetaBadge(
                    label = when {
                        lane.hasCapacityConflict -> "exact ${lane.configuredScope?.displayCapacityLabel}"
                        lane.configuredCapacity != null -> "exact ${lane.configuredCapacity}"
                        else -> "default 1"
                    },
                    accent = LaneAccent,
                    filled = lane.configuredCapacity != null || lane.hasCapacityConflict,
                )
                rootUsage?.let {
                    MetaBadge(
                        label = "shares ${scopeLabel(it)}=${it.displayCapacityLabel}",
                        accent = ScopeAccent,
                    )
                }
                if (lane.isEmulatorLike) {
                    MetaBadge(
                        label = if (emulatorMatched) "ADB ${lane.emulatorPort} matched" else "ADB ${lane.emulatorPort ?: "missing"} missing",
                        accent = if (emulatorMatched) LaneAccent else WarningAccent,
                        tooltip = lane.emulatorPort?.let { emulatorPort ->
                            if (emulatorMatched) {
                                "Queue lane `$emulatorPort` has a connected ADB emulator on the same port."
                            } else {
                                "Queue lane `$emulatorPort` is configured, but `adb devices -l` does not currently show a connected emulator on that port."
                            }
                        },
                    )
                }
            }

            if (lane.tasks.isEmpty()) {
                Text(
                    text = if (lane.configuredScope != null) "Idle configured lane." else "Idle lane.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.62f),
                )
            } else {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .horizontalScroll(rememberScrollState()),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    runningTasks.forEach { task ->
                        TaskChip(task = task, running = true)
                    }
                    if (runningTasks.isNotEmpty() && waitingTasks.isNotEmpty()) {
                        Text(
                            text = "queue",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.45f),
                        )
                    }
                    waitingTasks.forEach { task ->
                        TaskChip(task = task, running = false)
                    }
                }
            }
        }
    }
}

@Composable
private fun TaskChip(
    task: QueueTask,
    running: Boolean,
) {
    val accent = if (running) RunningAccent else WaitingAccent
    val background = if (running) accent.copy(alpha = 0.12f) else accent.copy(alpha = 0.05f)
    val identityLabel = task.displayIdentityLabel
    val diagnosticLabel = identityLabel ?: buildString {
        task.pid?.let { append("server $it") }
        task.childPid?.let {
            if (isNotEmpty()) append(" · ")
            append("child $it")
        }
    }.takeIf { it.isNotBlank() }

    Surface(
        shape = RoundedCornerShape(14.dp),
        color = background,
        tonalElevation = 0.dp,
    ) {
        Column(
            modifier = Modifier
                .widthIn(min = 220.dp, max = 300.dp)
                .border(1.dp, accent.copy(alpha = 0.34f), RoundedCornerShape(14.dp))
                .padding(horizontal = 10.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(3.dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                    StatusBadge(if (running) "run" else "wait", accent)
                    Text("#${task.id}", style = MaterialTheme.typography.labelLarge, fontWeight = FontWeight.SemiBold)
                }
                Text(
                    task.statusAge(),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.65f),
                )
            }

            Text(
                text = task.displayCommand,
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Medium,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )

            diagnosticLabel?.let {
                Text(
                    text = it,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.62f),
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}

@Composable
private fun EnvironmentDetailsSection(snapshot: QueueSnapshot) {
    SectionCard(
        title = "Environment Details",
        subtitle = "Secondary diagnostics for live server config and connected ADB devices.",
    ) {
        snapshot.statusMessage?.let { InfoBanner(it) }
        snapshot.configuration.statusMessage?.let { InfoBanner(it) }
        snapshot.adb.statusMessage?.let { InfoBanner(it) }

        Text(
            text = "Live queue servers",
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.SemiBold,
        )
        if (snapshot.configuration.serverProcesses.isEmpty()) {
            Text(
                text = "No matching task queue server processes detected for this data dir.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.65f),
            )
        } else {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                snapshot.configuration.serverProcesses.forEach { process ->
                    val identity = snapshot.serverIdentityByPid[process.pid]
                    Surface(
                        modifier = Modifier.fillMaxWidth(),
                        shape = RoundedCornerShape(12.dp),
                        color = MaterialTheme.colorScheme.surface,
                    ) {
                        Column(
                            modifier = Modifier
                                .fillMaxWidth()
                                .border(1.dp, MaterialTheme.colorScheme.outline.copy(alpha = 0.14f), RoundedCornerShape(12.dp))
                                .padding(horizontal = 12.dp, vertical = 10.dp),
                            verticalArrangement = Arrangement.spacedBy(6.dp),
                        ) {
                            Text(
                                text = identity?.displayLabel ?: process.agentLabel,
                                style = MaterialTheme.typography.titleSmall,
                                fontWeight = FontWeight.SemiBold,
                            )
                            identity?.launchContextLabel
                                ?.takeIf { identity.contextLabel == null }
                                ?.let { launchContextLabel ->
                                Text(
                                    text = "server launched from $launchContextLabel",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.58f),
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis,
                                )
                            }
                            Row(
                                modifier = Modifier.horizontalScroll(rememberScrollState()),
                                horizontalArrangement = Arrangement.spacedBy(6.dp),
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                MetaBadge(label = "pid ${process.pid}", accent = ScopeAccent)
                                MetaBadge(
                                    label = if (process.queueCapacities.isEmpty()) {
                                        "no scope overrides"
                                    } else {
                                        "${process.queueCapacities.size} scope override(s)"
                                    },
                                    accent = LaneAccent,
                                    filled = process.queueCapacities.isNotEmpty(),
                                )
                                identity?.detailLabel?.let { detailLabel ->
                                    MetaBadge(label = detailLabel, accent = WaitingAccent)
                                }
                            }
                            Text(
                                text = process.commandLine,
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.7f),
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                            )
                        }
                    }
                }
            }
        }

        Text(
            text = "ADB devices",
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.SemiBold,
        )
        if (snapshot.adb.devices.isEmpty()) {
            Text(
                text = "No ADB devices detected.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.65f),
            )
        } else {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                snapshot.adb.devices.forEach { device ->
                    AdbDeviceRow(device)
                }
            }
        }
    }
}

@Composable
private fun SummaryRow(snapshot: QueueSnapshot) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        SummaryCard(
            title = "Running",
            value = snapshot.summary.running.toString(),
            caption = "Active commands",
            accent = Color(0xFFD06A3A),
            modifier = Modifier.weight(1f),
        )
        SummaryCard(
            title = "Waiting",
            value = snapshot.summary.waiting.toString(),
            caption = "Queued tasks",
            accent = Color(0xFF3D7EA6),
            modifier = Modifier.weight(1f),
        )
        SummaryCard(
            title = "Visible Queues",
            value = snapshot.queueLanes.size.toString(),
            caption = "Observed + configured queue names",
            accent = Color(0xFF5B8A67),
            modifier = Modifier.weight(1f),
        )
        SummaryCard(
            title = "Emulators",
            value = "${snapshot.configuration.configuredEmulatorScopeCount} / ${snapshot.adb.connectedEmulators}",
            caption = "Configured emulator queues / connected emulators",
            accent = Color(0xFF8B5F8C),
            modifier = Modifier.weight(1f),
        )
    }
}

@Composable
private fun CapacityOverview(configuredScopes: List<ConfiguredScopeUsage>) {
    SectionCard(
        title = "Capacity Map",
        subtitle = "Configured scopes stay visible even when empty, so you can see where parallel slots actually exist.",
    ) {
        if (configuredScopes.isEmpty()) {
            Text(
                "No live --queue-capacity scopes detected. Exact queues still default to capacity 1.",
                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.65f),
            )
            return@SectionCard
        }

        Row(
            modifier = Modifier
                .fillMaxWidth()
                .horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            configuredScopes.forEach { usage ->
                Card(
                    modifier = Modifier.widthIn(min = 260.dp),
                    colors = CardDefaults.cardColors(containerColor = Color(0xFFF9F3EA)),
                ) {
                    Column(
                        modifier = Modifier.padding(16.dp),
                        verticalArrangement = Arrangement.spacedBy(10.dp),
                    ) {
                        Text(usage.scopeName, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                        Text(
                            text = if (usage.capacity != null) {
                                "${usage.runningCount} running · ${usage.waitingCount} waiting · ${usage.displayCapacityLabel} slot(s)"
                            } else {
                                "${usage.runningCount} running · ${usage.waitingCount} waiting · conflicting cap ${usage.displayCapacityLabel}"
                            },
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        SlotStrip(
                            capacity = usage.capacity,
                            usedSlots = usage.usedSlots,
                            accent = Color(0xFFB35C33),
                        )
                        Text(
                            text = "${usage.descendantQueueCount} visible queue(s) in scope · from ${usage.sourceServerLabels.joinToString()}",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.68f),
                            maxLines = 2,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun SummaryCard(
    title: String,
    value: String,
    caption: String,
    accent: Color,
    modifier: Modifier = Modifier,
) {
    Card(
        modifier = modifier,
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(18.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Box(
                modifier = Modifier
                    .clip(RoundedCornerShape(999.dp))
                    .background(accent.copy(alpha = 0.16f))
                    .padding(horizontal = 10.dp, vertical = 5.dp),
            ) {
                Text(title, color = accent, style = MaterialTheme.typography.labelLarge)
            }
            Text(value, style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
            Text(
                caption,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.7f),
            )
        }
    }
}

@Composable
private fun ScopeOverview(scopeGroups: List<ScopeGroup>) {
    if (scopeGroups.isEmpty()) {
        return
    }

    SectionCard(title = "Scope Activity", subtitle = "Each card rolls up descendant exact queues under a shared root scope.") {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            scopeGroups.forEach { scope ->
                Card(
                    modifier = Modifier.widthIn(min = 220.dp),
                    colors = CardDefaults.cardColors(containerColor = Color(0xFFF7F0E4)),
                ) {
                    Column(
                        modifier = Modifier.padding(16.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        Text(scope.scopeName, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                        Text(
                            text = "${scope.runningCount} running · ${scope.waitingCount} waiting",
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        Text(
                            text = "${scope.lanes.size} exact queues · ${scope.taskCount} total tasks",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.68f),
                        )
                        val configuredLanes = scope.lanes.count { it.configuredScope != null }
                        if (configuredLanes > 0) {
                            Text(
                                text = "$configuredLanes configured lane(s) visible even when idle",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.68f),
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun TaskSection(
    title: String,
    subtitle: String,
    tasks: List<QueueTask>,
    emptyLabel: String,
) {
    SectionCard(title = title, subtitle = subtitle) {
        if (tasks.isEmpty()) {
            Text(emptyLabel, color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.65f))
            return@SectionCard
        }

        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            tasks.forEach { task ->
                TaskRow(task = task, showQueue = true)
            }
        }
    }
}

@Composable
private fun ScopeDetails(scopeGroups: List<ScopeGroup>) {
    SectionCard(
        title = "Queues By Scope",
        subtitle = "Exact queues stay FIFO; grouping them here makes hierarchical queue families easier to scan.",
    ) {
        if (scopeGroups.isEmpty()) {
            Text("No active queues.", color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.65f))
            return@SectionCard
        }

        Column(verticalArrangement = Arrangement.spacedBy(18.dp)) {
            scopeGroups.forEach { scope ->
                Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    Text(scope.scopeName, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
                    scope.lanes.forEach { lane ->
                        QueueLaneCard(lane)
                    }
                }
            }
        }
    }
}

@Composable
private fun QueueLaneCard(lane: QueueLane) {
    Card(colors = CardDefaults.cardColors(containerColor = Color(0xFFFFFBF5))) {
        Column(modifier = Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(lane.queueName, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                    Text(
                        text = "${lane.runningCount} running · ${lane.waitingCount} waiting · ${lane.tasks.size} task(s)",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.7f),
                    )
                    Text(
                        text = when {
                            lane.hasCapacityConflict -> "Exact cap conflict: ${lane.configuredScope?.displayCapacityLabel}"
                            lane.configuredCapacity != null -> "Exact cap ${lane.configuredCapacity} configured"
                            else -> "Exact cap 1 default"
                        },
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.68f),
                    )
                    if (lane.configuredCapacity != null || lane.hasCapacityConflict) {
                        SlotStrip(
                            capacity = lane.configuredCapacity,
                            usedSlots = lane.runningCount,
                            accent = Color(0xFF46705C),
                        )
                    }
                }
            }

            HorizontalDivider(color = MaterialTheme.colorScheme.outline.copy(alpha = 0.3f))

            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (lane.tasks.isEmpty()) {
                    Text(
                        text = "No live tasks in this queue right now.",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.65f),
                    )
                } else {
                    lane.tasks.forEach { task ->
                        TaskRow(task = task, showQueue = false)
                    }
                }
            }
        }
    }
}

@Composable
private fun TaskRow(task: QueueTask, showQueue: Boolean) {
    val accent = if (task.status.equals("running", ignoreCase = true)) {
        Color(0xFFD06A3A)
    } else {
        Color(0xFF3D7EA6)
    }

    Surface(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(18.dp),
        color = MaterialTheme.colorScheme.surface,
        tonalElevation = 1.dp,
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .border(1.dp, accent.copy(alpha = 0.18f), RoundedCornerShape(18.dp))
                .padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                    StatusBadge(task.status, accent)
                    Text("#${task.id}", fontWeight = FontWeight.Medium)
                    if (showQueue) {
                        Text(
                            task.queueName,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.7f),
                        )
                    }
                }
                Text(
                    task.statusAge(),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.7f),
                )
            }

            Text(
                text = task.displayCommand,
                style = MaterialTheme.typography.bodyLarge,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )

            val processLine = buildString {
                task.displayIdentityLabel?.let { append(it) }
                task.pid?.takeIf { task.displayIdentityLabel == null }?.let { append("server pid $it") }
                task.childPid?.let {
                    if (isNotEmpty()) append(" · ")
                    append("child pid $it")
                }
            }
            if (processLine.isNotEmpty()) {
                Text(
                    processLine,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f),
                )
            }
        }
    }
}

@Composable
private fun AdbSection(snapshot: QueueSnapshot) {
    SectionCard(
        title = "ADB Devices",
        subtitle = "Compare connected emulators with queue scopes so emulator fan-out stays aligned with real devices.",
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            SummaryCard(
                title = "Connected",
                value = snapshot.adb.connectedDevices.toString(),
                caption = "ADB devices in state=device",
                accent = Color(0xFF305B78),
                modifier = Modifier.weight(1f),
            )
            SummaryCard(
                title = "Emulators",
                value = snapshot.adb.connectedEmulators.toString(),
                caption = "Connected emulator serials",
                accent = Color(0xFFB35C33),
                modifier = Modifier.weight(1f),
            )
            SummaryCard(
                title = "Configured",
                value = snapshot.emulatorAlignment.configuredQueues.size.toString(),
                caption = "Configured emulator queues",
                accent = Color(0xFF46705C),
                modifier = Modifier.weight(1f),
            )
            SummaryCard(
                title = "Matched",
                value = snapshot.emulatorAlignment.matchedPorts.size.toString(),
                caption = "Queue/device port matches",
                accent = Color(0xFF8B5F8C),
                modifier = Modifier.weight(1f),
            )
        }

        val alignment = snapshot.emulatorAlignment
        if (alignment.unmatchedConfiguredQueues.isNotEmpty() || alignment.unmatchedDevices.isNotEmpty()) {
            Text(
                text = buildString {
                    if (alignment.unmatchedConfiguredQueues.isNotEmpty()) {
                        append("Unmatched configured queues: ")
                        append(alignment.unmatchedConfiguredQueues.joinToString { it.queueName })
                    }
                    if (alignment.unmatchedDevices.isNotEmpty()) {
                        if (isNotEmpty()) append(". ")
                        append("Unmatched ADB devices: ")
                        append(alignment.unmatchedDevices.joinToString { it.serial })
                    }
                },
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.72f),
            )
        }

        if (snapshot.adb.devices.isEmpty()) {
            Text(
                text = "No ADB devices to display.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.65f),
            )
            return@SectionCard
        }

        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            snapshot.adb.devices.forEach { device ->
                AdbDeviceRow(device)
            }
        }
    }
}

@Composable
private fun StatusBadge(text: String, accent: Color) {
    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(999.dp))
            .background(accent.copy(alpha = 0.16f))
            .padding(horizontal = 8.dp, vertical = 3.dp),
    ) {
        Text(
            text = text.uppercase(),
            color = accent,
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.SemiBold,
        )
    }
}

@Composable
@OptIn(ExperimentalFoundationApi::class)
private fun MetaBadge(
    label: String,
    accent: Color,
    filled: Boolean = false,
    tooltip: String? = null,
) {
    val background = if (filled) accent.copy(alpha = 0.14f) else Color.Transparent
    val badgeContent: @Composable () -> Unit = {
        Surface(
            shape = RoundedCornerShape(999.dp),
            color = background,
        ) {
            Text(
                text = label,
                modifier = Modifier
                    .border(1.dp, accent.copy(alpha = 0.38f), RoundedCornerShape(999.dp))
                    .padding(horizontal = 9.dp, vertical = 4.dp),
                color = accent,
                style = MaterialTheme.typography.labelSmall,
            )
        }
    }

    if (tooltip != null) {
        TooltipArea(
            tooltip = {
                TooltipBubble(tooltip)
            },
            delayMillis = 150,
        ) {
            badgeContent()
        }
    } else {
        badgeContent()
    }
}

@Composable
private fun TooltipBubble(text: String) {
    Surface(
        shape = RoundedCornerShape(10.dp),
        color = TooltipColor,
        shadowElevation = 8.dp,
    ) {
        Text(
            text = text,
            modifier = Modifier
                .widthIn(max = 280.dp)
                .padding(horizontal = 10.dp, vertical = 8.dp),
            color = Color.White,
            style = MaterialTheme.typography.bodySmall,
        )
    }
}

@Composable
private fun SlotStrip(
    capacity: Int?,
    usedSlots: Int,
    accent: Color,
    fallbackLabel: String? = null,
) {
    if (capacity == null) {
        fallbackLabel?.let {
            Text(
                text = it,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.65f),
            )
        }
        return
    }

    Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
        repeat(capacity) { index ->
            val filled = index < usedSlots
            Box(
                modifier = Modifier
                    .size(width = 16.dp, height = 9.dp)
                    .clip(RoundedCornerShape(4.dp))
                    .background(if (filled) accent else accent.copy(alpha = 0.12f))
                    .border(1.dp, accent.copy(alpha = 0.4f), RoundedCornerShape(4.dp)),
            )
        }
        Text(
            text = "$usedSlots/$capacity used",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.7f),
        )
    }
}

private fun scopeLabel(usage: ConfiguredScopeUsage): String = usage.scopeName.substringAfterLast('/')

@Composable
private fun AdbDeviceRow(device: AdbDevice) {
    val accent = when {
        device.isConnected && device.isEmulator -> Color(0xFF46705C)
        device.isConnected -> Color(0xFF305B78)
        else -> Color(0xFFB35C33)
    }

    Surface(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(14.dp),
        color = MaterialTheme.colorScheme.surface,
        tonalElevation = 1.dp,
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .border(1.dp, accent.copy(alpha = 0.18f), RoundedCornerShape(14.dp))
                .padding(horizontal = 12.dp, vertical = 10.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                    StatusBadge(device.state, accent)
                    Text(device.serial, fontWeight = FontWeight.Medium)
                }
                if (device.emulatorPort != null) {
                    Text(
                        text = "port ${device.emulatorPort}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.7f),
                    )
                }
            }
            if (device.detailLine.isNotBlank()) {
                Text(
                    text = device.detailLine,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.68f),
                )
            }
        }
    }
}

@Composable
private fun SectionCard(
    title: String,
    subtitle: String,
    content: @Composable ColumnScope.() -> Unit,
) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Column(
            modifier = Modifier.fillMaxWidth().padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
            content = {
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(title, style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.SemiBold)
                    Text(
                        subtitle,
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.7f),
                    )
                }
                content()
            },
        )
    }
}

@Composable
private fun ErrorBanner(message: String) {
    Banner(message = message, background = Color(0xFFFBE4E3), foreground = MaterialTheme.colorScheme.error)
}

@Composable
private fun InfoBanner(message: String) {
    Banner(message = message, background = Color(0xFFEAF1F6), foreground = MaterialTheme.colorScheme.primary)
}

@Composable
private fun Banner(message: String, background: Color, foreground: Color) {
    Surface(shape = RoundedCornerShape(16.dp), color = background) {
        Text(
            text = message,
            modifier = Modifier.fillMaxWidth().padding(14.dp),
            color = foreground,
        )
    }
}

private fun resolveDataDir(args: Array<String>): Path {
    if (args.any { it == "-h" || it == "--help" }) {
        println("Usage: ./gradlew run --args=\"[--data-dir PATH]\"")
        exitProcess(0)
    }

    var configuredPath: String? = null
    var index = 0
    while (index < args.size) {
        val arg = args[index]
        when {
            arg.startsWith("--data-dir=") -> configuredPath = arg.substringAfter("=")
            arg == "--data-dir" && index + 1 < args.size -> {
                configuredPath = args[index + 1]
                index += 1
            }
        }
        index += 1
    }

    val fallback = System.getenv("TASK_QUEUE_DATA_DIR")?.takeIf { it.isNotBlank() }
        ?: "/tmp/agent-task-queue"
    return Paths.get(configuredPath ?: fallback)
}
