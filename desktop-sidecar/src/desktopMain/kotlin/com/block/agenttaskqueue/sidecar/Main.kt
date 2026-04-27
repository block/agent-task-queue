package com.block.agenttaskqueue.sidecar

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.TooltipArea
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Window
import androidx.compose.ui.window.application
import androidx.compose.ui.window.rememberWindowState
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.nio.file.Path
import java.nio.file.Paths
import kotlin.math.max
import kotlin.system.exitProcess

private const val ACTIVE_INTERVAL_MS = 1000L
private const val IDLE_INTERVAL_MS = 3000L

// Neutral surfaces
private val Background = Color(0xFFF4EEE5)
private val BackgroundGradientEnd = Color(0xFFEDE3D4)
private val SurfaceCard = Color(0xFFFFFCF7)
private val SurfaceElevated = Color(0xFFFFFFFF)
private val DividerColor = Color(0x14000000)

// Text
private val TextPrimary = Color(0xFF1F262D)
private val TextSecondary = Color(0xFF5E6670)
private val TextMuted = Color(0xFF8A9099)

// Status accents
private val AccentRunning = Color(0xFFC96A3D)
private val AccentWaiting = Color(0xFF3F7698)
private val AccentSuccess = Color(0xFF4E8A5A)
private val AccentWarning = Color(0xFFD08A2E)
private val AccentDanger = Color(0xFFB8472E)
private val AccentIdle = Color(0xFFB0A99E)

private val TooltipColor = Color(0xFF2B2F35)

// Stable per-agent color palette so a developer running multiple agents can
// visually pick out "which agent is that" at a glance.
private val AgentPalette = linkedMapOf(
    "Amp" to Color(0xFF2A7E76),
    "Claude" to Color(0xFFB8742E),
    "Codex" to Color(0xFF6B4AA8),
    "Cursor" to Color(0xFFA83F6C),
    "Zed" to Color(0xFF2E6BA8),
    "Windsurf" to Color(0xFF5E8A2E),
)
private val UnknownAgentColor = Color(0xFF5A6370)

private fun agentColor(label: String?): Color {
    if (label.isNullOrBlank()) return UnknownAgentColor
    AgentPalette.forEach { (name, color) ->
        if (label.startsWith(name, ignoreCase = true)) return color
    }
    return UnknownAgentColor
}

private val DashboardColors = lightColorScheme(
    primary = Color(0xFF305B78),
    secondary = AccentRunning,
    tertiary = AccentSuccess,
    background = Background,
    surface = SurfaceCard,
    surfaceVariant = Color(0xFFE9DFCF),
    onBackground = TextPrimary,
    onSurface = TextPrimary,
    outline = Color(0xFF877F74),
    error = AccentDanger,
)

fun main(args: Array<String>) = application {
    val dataDir = resolveDataDir(args)

    Window(
        onCloseRequest = ::exitApplication,
        title = "Agent Task Queue Sidecar",
        state = rememberWindowState(width = 1440.dp, height = 920.dp),
    ) {
        MaterialTheme(colorScheme = DashboardColors) {
            QueueDashboard(dataDir = dataDir)
        }
    }
}

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
        containerColor = MaterialTheme.colorScheme.background,
        topBar = { DashboardTopBar(snapshot) { refreshRequests.trySend(Unit) } },
    ) { innerPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .background(
                    Brush.verticalGradient(listOf(Background, BackgroundGradientEnd))
                )
                .padding(innerPadding)
                .padding(horizontal = 20.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            HeroStatStrip(snapshot)
            BannerStack(snapshot)
            Row(
                modifier = Modifier.fillMaxWidth().weight(1f, fill = true),
                horizontalArrangement = Arrangement.spacedBy(14.dp),
            ) {
                Column(
                    modifier = Modifier
                        .weight(1f)
                        .fillMaxHeight()
                        .verticalScroll(rememberScrollState()),
                    verticalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    QueueActivityPane(snapshot)
                    LegendFooter()
                }
                Column(
                    modifier = Modifier
                        .width(400.dp)
                        .fillMaxHeight()
                        .verticalScroll(rememberScrollState()),
                    verticalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    AgentsPanel(snapshot)
                    EmulatorsPanel(snapshot)
                    ServersPanel(snapshot)
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DashboardTopBar(snapshot: QueueSnapshot, onRefresh: () -> Unit) {
    TopAppBar(
        title = {
            Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text("Agent Task Queue", fontWeight = FontWeight.SemiBold)
                Text(
                    text = snapshot.dataDir.toString(),
                    style = MaterialTheme.typography.bodySmall,
                    color = TextSecondary,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        },
        actions = {
            Text(
                text = "Updated ${formatRefreshTime(snapshot.refreshedAt)}",
                style = MaterialTheme.typography.bodySmall,
                color = TextSecondary,
            )
            Spacer(Modifier.width(12.dp))
            Button(onClick = onRefresh) { Text("Refresh") }
            Spacer(Modifier.width(16.dp))
        },
    )
}

// ---------- Hero strip ----------

@Composable
private fun HeroStatStrip(snapshot: QueueSnapshot) {
    val running = snapshot.summary.running
    val waiting = snapshot.summary.waiting
    val activeAgents = snapshot.runningTasks
        .mapNotNull { it.displayAgentLabel }
        .distinct()
        .size
    val configuredEmu = snapshot.emulatorAlignment.configuredQueues.size
    val matchedEmu = snapshot.emulatorAlignment.matchedPorts.size
    val connectedEmu = snapshot.adb.connectedEmulators

    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        StatTile(
            modifier = Modifier.weight(1f),
            label = "RUNNING",
            value = running.toString(),
            caption = if (running == 0) "Nothing active" else if (running == 1) "Active task" else "Active tasks",
            accent = if (running > 0) AccentRunning else AccentIdle,
        )
        StatTile(
            modifier = Modifier.weight(1f),
            label = "WAITING",
            value = waiting.toString(),
            caption = when {
                waiting == 0 -> "Queue clear"
                waiting >= 5 -> "Queue backed up"
                waiting == 1 -> "Queued task"
                else -> "Queued tasks"
            },
            accent = when {
                waiting >= 5 -> AccentDanger
                waiting > 0 -> AccentWarning
                else -> AccentIdle
            },
        )
        StatTile(
            modifier = Modifier.weight(1f),
            label = "AGENTS",
            value = activeAgents.toString(),
            caption = if (activeAgents == 0) "No agents running" else "With running tasks",
            accent = if (activeAgents > 0) AccentSuccess else AccentIdle,
        )
        StatTile(
            modifier = Modifier.weight(1f),
            label = "EMULATORS",
            value = if (configuredEmu == 0) connectedEmu.toString() else "$matchedEmu/$configuredEmu",
            caption = when {
                configuredEmu == 0 && connectedEmu == 0 -> "None connected"
                configuredEmu == 0 -> "Connected, no lanes"
                matchedEmu < configuredEmu -> "Lane missing device"
                else -> "Lanes matched"
            },
            accent = when {
                configuredEmu > 0 && matchedEmu < configuredEmu -> AccentDanger
                configuredEmu == 0 && connectedEmu == 0 -> AccentIdle
                else -> AccentSuccess
            },
        )
    }
}

@Composable
private fun StatTile(
    modifier: Modifier,
    label: String,
    value: String,
    caption: String,
    accent: Color,
) {
    Card(
        modifier = modifier,
        colors = CardDefaults.cardColors(containerColor = SurfaceElevated),
        elevation = CardDefaults.cardElevation(defaultElevation = 1.dp),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .height(IntrinsicSize.Min)
                .padding(horizontal = 14.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                modifier = Modifier
                    .width(4.dp)
                    .height(44.dp)
                    .clip(RoundedCornerShape(2.dp))
                    .background(accent),
            )
            Spacer(Modifier.width(12.dp))
            Column(verticalArrangement = Arrangement.spacedBy(1.dp)) {
                Text(
                    text = label,
                    style = MaterialTheme.typography.labelSmall,
                    color = accent,
                    fontWeight = FontWeight.SemiBold,
                    letterSpacing = 1.2.sp,
                )
                Text(
                    text = value,
                    style = MaterialTheme.typography.headlineLarge,
                    fontWeight = FontWeight.Bold,
                    color = TextPrimary,
                )
                Text(
                    text = caption,
                    style = MaterialTheme.typography.bodySmall,
                    color = TextSecondary,
                )
            }
        }
    }
}

// ---------- Queue activity pane ----------

@Composable
private fun QueueActivityPane(snapshot: QueueSnapshot) {
    if (snapshot.scopeGroups.isEmpty()) {
        EmptyState(
            title = "No queue activity",
            message = snapshot.statusMessage ?: "No queues are visible yet.",
        )
        return
    }

    val sortedScopes = snapshot.scopeGroups.sortedWith(
        compareByDescending<ScopeGroup> { it.waitingCount > 0 }
            .thenByDescending { scopePressure(it, snapshot) }
            .thenByDescending { it.runningCount }
            .thenBy { it.scopeName }
    )
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        sortedScopes.forEach { scope ->
            ScopeCard(scope = scope, snapshot = snapshot)
        }
    }
}

private fun scopePressure(scope: ScopeGroup, snapshot: QueueSnapshot): Double {
    val usage = snapshot.configuredScopeUsage.firstOrNull { it.scopeName == scope.scopeName }
    val cap = usage?.capacity
    if (cap == null || cap == 0) {
        return if (scope.runningCount == 0) 0.0 else 1.0
    }
    return scope.runningCount.toDouble() / cap
}

@Composable
private fun ScopeCard(scope: ScopeGroup, snapshot: QueueSnapshot) {
    val usage = snapshot.configuredScopeUsage.firstOrNull { it.scopeName == scope.scopeName }
    val capacity = usage?.capacity
    val used = usage?.usedSlots ?: scope.runningCount
    val hasBackup = scope.waitingCount > 0
    val capFull = capacity != null && used >= capacity && capacity > 0

    val accent = when {
        hasBackup && capFull -> AccentDanger
        hasBackup -> AccentWarning
        capFull -> AccentWarning
        used > 0 -> AccentRunning
        else -> AccentIdle
    }

    Card(
        colors = CardDefaults.cardColors(containerColor = SurfaceCard),
        elevation = CardDefaults.cardElevation(defaultElevation = 1.dp),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .height(IntrinsicSize.Min),
        ) {
            Box(
                modifier = Modifier
                    .width(5.dp)
                    .fillMaxHeight()
                    .background(accent),
            )
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 16.dp, vertical = 14.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                ScopeHeader(
                    scope = scope,
                    usage = usage,
                    accent = accent,
                    hasBackup = hasBackup,
                    capFull = capFull,
                )
                val sortedLanes = scope.lanes.sortedWith(
                    compareByDescending<QueueLane> { it.waitingCount > 0 }
                        .thenByDescending { it.runningCount > 0 }
                        .thenByDescending { it.configuredScope != null }
                        .thenBy { it.queueName }
                )
                Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    sortedLanes.forEach { lane ->
                        val matched = lane.emulatorPort != null &&
                            lane.emulatorPort in snapshot.emulatorAlignment.matchedPorts
                        LaneRow(lane = lane, emulatorMatched = matched)
                    }
                }
            }
        }
    }
}

@Composable
private fun ScopeHeader(
    scope: ScopeGroup,
    usage: ConfiguredScopeUsage?,
    accent: Color,
    hasBackup: Boolean,
    capFull: Boolean,
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.Top,
    ) {
        Column(
            modifier = Modifier.weight(1f),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Text(
                    text = scope.scopeName,
                    style = MaterialTheme.typography.headlineSmall,
                    fontWeight = FontWeight.SemiBold,
                )
                if (hasBackup) {
                    PressureChip("BACKUP", AccentDanger)
                }
                if (capFull && !hasBackup) {
                    PressureChip("AT CAPACITY", AccentWarning)
                }
            }
            Text(
                text = "${scope.lanes.size} lane${if (scope.lanes.size == 1) "" else "s"} · " +
                    "${scope.runningCount} running · ${scope.waitingCount} waiting",
                style = MaterialTheme.typography.bodySmall,
                color = TextSecondary,
            )
        }
        CapacityMeter(
            capacity = usage?.capacity,
            used = usage?.usedSlots ?: scope.runningCount,
            accent = accent,
            configured = usage != null,
        )
    }
}

@Composable
private fun CapacityMeter(
    capacity: Int?,
    used: Int,
    accent: Color,
    configured: Boolean,
) {
    Column(
        horizontalAlignment = Alignment.End,
        verticalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        if (capacity == null) {
            Text(
                text = if (configured) "cap conflict" else "default per-lane",
                style = MaterialTheme.typography.labelSmall,
                color = TextMuted,
            )
            return
        }
        Text(
            text = "$used / $capacity slots",
            style = MaterialTheme.typography.labelLarge,
            fontWeight = FontWeight.SemiBold,
            color = accent,
        )
        Row(horizontalArrangement = Arrangement.spacedBy(3.dp)) {
            repeat(capacity) { i ->
                Box(
                    modifier = Modifier
                        .size(width = 18.dp, height = 10.dp)
                        .clip(RoundedCornerShape(3.dp))
                        .background(if (i < used) accent else accent.copy(alpha = 0.15f))
                        .border(1.dp, accent.copy(alpha = 0.35f), RoundedCornerShape(3.dp)),
                )
            }
        }
    }
}

@Composable
private fun LaneRow(lane: QueueLane, emulatorMatched: Boolean) {
    val leaf = lane.queueName.substringAfterLast('/')
    val showFullPath = leaf != lane.queueName
    val cap = lane.exactCapacity
    val running = lane.tasks.filter { it.status.equals("running", ignoreCase = true) }
    val waiting = lane.tasks.filter { it.status.equals("waiting", ignoreCase = true) }
    val hasBackup = waiting.isNotEmpty()
    val accent = when {
        hasBackup -> AccentWarning
        running.isNotEmpty() -> AccentRunning
        else -> AccentIdle
    }

    Surface(
        shape = RoundedCornerShape(14.dp),
        color = SurfaceElevated,
        tonalElevation = 0.dp,
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .height(IntrinsicSize.Min)
                .border(1.dp, accent.copy(alpha = 0.2f), RoundedCornerShape(14.dp)),
        ) {
            Box(
                modifier = Modifier
                    .width(3.dp)
                    .fillMaxHeight()
                    .background(accent),
            )
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 12.dp, vertical = 10.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(
                        modifier = Modifier.weight(1f),
                        verticalArrangement = Arrangement.spacedBy(1.dp),
                    ) {
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            Text(
                                text = leaf,
                                style = MaterialTheme.typography.titleSmall,
                                fontWeight = FontWeight.SemiBold,
                            )
                            if (lane.isEmulatorLike) {
                                EmulatorDot(
                                    matched = emulatorMatched,
                                    port = lane.emulatorPort,
                                )
                            }
                            if (lane.hasCapacityConflict) {
                                PressureChip("CAP CONFLICT", AccentDanger)
                            }
                        }
                        if (showFullPath) {
                            Text(
                                text = lane.queueName,
                                style = MaterialTheme.typography.labelSmall,
                                color = TextMuted,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                            )
                        }
                    }
                    LaneCountBadge(running = running.size, waiting = waiting.size, cap = cap)
                }

                LaneTimeline(cap = cap, running = running, waiting = waiting)
            }
        }
    }
}

@Composable
private fun LaneCountBadge(running: Int, waiting: Int, cap: Int) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        if (running > 0) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Box(
                    Modifier
                        .size(7.dp)
                        .clip(CircleShape)
                        .background(AccentRunning),
                )
                Text(
                    text = "$running/$cap",
                    style = MaterialTheme.typography.labelSmall,
                    color = TextSecondary,
                    fontWeight = FontWeight.Medium,
                )
            }
        }
        if (waiting > 0) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Box(
                    Modifier
                        .size(7.dp)
                        .clip(CircleShape)
                        .background(AccentWarning),
                )
                Text(
                    text = "+$waiting",
                    style = MaterialTheme.typography.labelSmall,
                    color = TextSecondary,
                    fontWeight = FontWeight.Medium,
                )
            }
        }
        if (running == 0 && waiting == 0) {
            Text(
                text = "idle",
                style = MaterialTheme.typography.labelSmall,
                color = TextMuted,
            )
        }
    }
}

@Composable
private fun LaneTimeline(cap: Int, running: List<QueueTask>, waiting: List<QueueTask>) {
    if (running.isEmpty() && waiting.isEmpty()) {
        Text(
            text = "Lane idle",
            style = MaterialTheme.typography.bodySmall,
            color = TextMuted,
            fontStyle = FontStyle.Italic,
        )
        return
    }
    val slotCount = max(cap, running.size).coerceAtLeast(1)
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .horizontalScroll(rememberScrollState()),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        repeat(slotCount) { i ->
            val task = running.getOrNull(i)
            if (task != null) {
                TaskPill(task = task, running = true)
            } else {
                EmptySlotPill()
            }
        }
        if (waiting.isNotEmpty()) {
            Box(
                modifier = Modifier
                    .width(1.dp)
                    .height(44.dp)
                    .background(DividerColor),
            )
            waiting.forEach { task ->
                TaskPill(task = task, running = false)
            }
        }
    }
}

@Composable
private fun EmptySlotPill() {
    Box(
        modifier = Modifier
            .size(width = 200.dp, height = 62.dp)
            .clip(RoundedCornerShape(10.dp))
            .background(Color(0xFFF2ECE0))
            .border(1.dp, DividerColor, RoundedCornerShape(10.dp)),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = "open slot",
            style = MaterialTheme.typography.labelSmall,
            color = TextMuted,
            fontStyle = FontStyle.Italic,
        )
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun TaskPill(task: QueueTask, running: Boolean) {
    val accent = agentColor(task.displayAgentLabel)
    val bg = if (running) accent.copy(alpha = 0.14f) else accent.copy(alpha = 0.06f)
    val border = if (running) accent.copy(alpha = 0.55f) else accent.copy(alpha = 0.3f)

    TooltipArea(
        tooltip = { TooltipBubble(buildTaskTooltip(task)) },
        delayMillis = 200,
    ) {
        Column(
            modifier = Modifier
                .widthIn(min = 200.dp, max = 260.dp)
                .clip(RoundedCornerShape(10.dp))
                .background(bg)
                .border(1.dp, border, RoundedCornerShape(10.dp))
                .padding(horizontal = 10.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(3.dp),
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                Box(
                    Modifier
                        .size(8.dp)
                        .clip(CircleShape)
                        .background(accent),
                )
                Text(
                    text = task.displayAgentLabel ?: "unknown agent",
                    style = MaterialTheme.typography.labelSmall,
                    color = accent,
                    fontWeight = FontWeight.SemiBold,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f, fill = false),
                )
                Spacer(Modifier.weight(1f))
                Text(
                    text = "#${task.id}",
                    style = MaterialTheme.typography.labelSmall,
                    color = TextSecondary,
                )
            }
            Text(
                text = task.displayCommand,
                style = MaterialTheme.typography.bodySmall,
                fontWeight = if (running) FontWeight.Medium else FontWeight.Normal,
                color = TextPrimary,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                task.displayContextLabel?.let {
                    Text(
                        text = it,
                        style = MaterialTheme.typography.labelSmall,
                        color = TextMuted,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f, fill = false),
                    )
                }
                Spacer(Modifier.weight(1f))
                Text(
                    text = task.statusAge(),
                    style = MaterialTheme.typography.labelSmall,
                    color = TextMuted,
                )
            }
        }
    }
}

private fun buildTaskTooltip(task: QueueTask): String = buildString {
    append("#${task.id}  ")
    append(task.status.uppercase())
    append('\n')
    append(task.displayCommand)
    task.displayIdentityLabel?.let {
        append("\n\n")
        append(it)
    }
    append("\n\nQueue: ")
    append(task.queueName)
    append('\n')
    append(task.statusAge())
    val pidParts = buildList {
        task.pid?.let { add("server pid $it") }
        task.childPid?.let { add("child pid $it") }
    }
    if (pidParts.isNotEmpty()) {
        append('\n')
        append(pidParts.joinToString(" · "))
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun EmulatorDot(matched: Boolean, port: String?) {
    val color = if (matched) AccentSuccess else AccentDanger
    val tooltip = if (matched) {
        "Queue lane maps to ADB emulator on port ${port ?: "?"}."
    } else {
        "Lane expects emulator on port ${port ?: "?"}, but `adb devices -l` does not show one."
    }
    TooltipArea(
        tooltip = { TooltipBubble(tooltip) },
        delayMillis = 200,
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(4.dp),
            modifier = Modifier
                .clip(RoundedCornerShape(999.dp))
                .background(color.copy(alpha = 0.12f))
                .padding(horizontal = 6.dp, vertical = 2.dp),
        ) {
            Box(
                Modifier
                    .size(6.dp)
                    .clip(CircleShape)
                    .background(color),
            )
            Text(
                text = if (matched) "ADB :${port ?: "?"}" else "ADB missing",
                style = MaterialTheme.typography.labelSmall,
                color = color,
                fontWeight = FontWeight.Medium,
            )
        }
    }
}

@Composable
private fun PressureChip(text: String, accent: Color) {
    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(999.dp))
            .background(accent.copy(alpha = 0.15f))
            .border(1.dp, accent.copy(alpha = 0.55f), RoundedCornerShape(999.dp))
            .padding(horizontal = 8.dp, vertical = 3.dp),
    ) {
        Text(
            text = text,
            color = accent,
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.SemiBold,
            letterSpacing = 0.8.sp,
        )
    }
}

// ---------- Environment pane: agents ----------

private data class AgentContextSummary(
    val context: String,
    val runningCount: Int,
    val waitingCount: Int,
)

private data class AgentSummary(
    val agentLabel: String,
    val contexts: List<AgentContextSummary>,
    val runningTotal: Int,
    val waitingTotal: Int,
)

private fun buildAgentSummaries(snapshot: QueueSnapshot): List<AgentSummary> {
    return snapshot.tasks
        .groupBy { it.displayAgentLabel ?: "Unknown agent" }
        .map { (agentLabel, tasks) ->
            val contexts = tasks
                .groupBy { it.displayContextLabel ?: "no context" }
                .map { (ctx, ctxTasks) ->
                    AgentContextSummary(
                        context = ctx,
                        runningCount = ctxTasks.count { it.status.equals("running", ignoreCase = true) },
                        waitingCount = ctxTasks.count { it.status.equals("waiting", ignoreCase = true) },
                    )
                }
                .sortedWith(
                    compareByDescending<AgentContextSummary> { it.runningCount }
                        .thenByDescending { it.waitingCount }
                        .thenBy { it.context }
                )
            AgentSummary(
                agentLabel = agentLabel,
                contexts = contexts,
                runningTotal = tasks.count { it.status.equals("running", ignoreCase = true) },
                waitingTotal = tasks.count { it.status.equals("waiting", ignoreCase = true) },
            )
        }
        .sortedWith(
            compareByDescending<AgentSummary> { it.runningTotal }
                .thenByDescending { it.waitingTotal }
                .thenBy { it.agentLabel }
        )
}

@Composable
private fun AgentsPanel(snapshot: QueueSnapshot) {
    val summaries = buildAgentSummaries(snapshot)
    PaneSection(
        title = "Agents",
        subtitle = when {
            summaries.isEmpty() -> "No agent activity"
            summaries.size == 1 -> "1 agent with live tasks"
            else -> "${summaries.size} agents with live tasks"
        },
    ) {
        if (summaries.isEmpty()) {
            Text(
                text = "No running or queued tasks.",
                style = MaterialTheme.typography.bodySmall,
                color = TextMuted,
            )
            return@PaneSection
        }
        summaries.forEach { AgentCard(it) }
    }
}

@Composable
private fun AgentCard(summary: AgentSummary) {
    val accent = agentColor(summary.agentLabel)
    Surface(
        shape = RoundedCornerShape(12.dp),
        color = SurfaceElevated,
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .height(IntrinsicSize.Min)
                .border(1.dp, accent.copy(alpha = 0.22f), RoundedCornerShape(12.dp)),
        ) {
            Box(
                modifier = Modifier
                    .width(4.dp)
                    .fillMaxHeight()
                    .background(accent),
            )
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 12.dp, vertical = 10.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(
                        Modifier
                            .size(10.dp)
                            .clip(CircleShape)
                            .background(accent),
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(
                        text = summary.agentLabel,
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.SemiBold,
                        color = accent,
                        modifier = Modifier.weight(1f),
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    AgentCountBadges(
                        running = summary.runningTotal,
                        waiting = summary.waitingTotal,
                    )
                }
                if (summary.contexts.isNotEmpty()) {
                    Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
                        summary.contexts.forEach { ctx ->
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(6.dp),
                            ) {
                                Box(
                                    Modifier
                                        .size(5.dp)
                                        .clip(CircleShape)
                                        .background(accent.copy(alpha = 0.45f)),
                                )
                                Text(
                                    text = ctx.context,
                                    style = MaterialTheme.typography.bodySmall,
                                    color = TextPrimary,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis,
                                    modifier = Modifier.weight(1f),
                                )
                                if (ctx.runningCount > 0) {
                                    Text(
                                        text = "${ctx.runningCount} run",
                                        style = MaterialTheme.typography.labelSmall,
                                        color = AccentRunning,
                                        fontWeight = FontWeight.Medium,
                                    )
                                }
                                if (ctx.waitingCount > 0) {
                                    Text(
                                        text = "${ctx.waitingCount} wait",
                                        style = MaterialTheme.typography.labelSmall,
                                        color = AccentWarning,
                                        fontWeight = FontWeight.Medium,
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun AgentCountBadges(running: Int, waiting: Int) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        CountBubble(label = running.toString(), accent = AccentRunning, caption = "run")
        CountBubble(label = waiting.toString(), accent = AccentWarning, caption = "wait")
    }
}

@Composable
private fun CountBubble(label: String, accent: Color, caption: String) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(3.dp),
        modifier = Modifier
            .clip(RoundedCornerShape(999.dp))
            .background(accent.copy(alpha = 0.12f))
            .padding(horizontal = 7.dp, vertical = 2.dp),
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.labelMedium,
            color = accent,
            fontWeight = FontWeight.SemiBold,
        )
        Text(
            text = caption,
            style = MaterialTheme.typography.labelSmall,
            color = accent.copy(alpha = 0.75f),
        )
    }
}

// ---------- Environment pane: emulators ----------

@Composable
private fun EmulatorsPanel(snapshot: QueueSnapshot) {
    val alignment = snapshot.emulatorAlignment
    val matchedPorts = alignment.matchedPorts
    val configured = alignment.configuredQueues
    val connected = snapshot.adb.devices.filter { it.isConnected && it.isEmulator }
    val devicesByPort = connected.associateBy { it.emulatorPort }
    val extraDevices = connected.filter { it.emulatorPort == null || it.emulatorPort !in matchedPorts }

    PaneSection(
        title = "Emulators",
        subtitle = when {
            configured.isEmpty() && connected.isEmpty() -> "No configured lanes · no emulators"
            configured.isEmpty() -> "${connected.size} emulator(s) connected · no lanes configured"
            else -> "${matchedPorts.size}/${configured.size} lanes matched to devices"
        },
    ) {
        if (configured.isEmpty() && connected.isEmpty()) {
            Text(
                text = "Nothing to show. Start an emulator or configure an emulator queue lane.",
                style = MaterialTheme.typography.bodySmall,
                color = TextMuted,
            )
            return@PaneSection
        }

        val sortedConfigured = configured.sortedWith(
            compareByDescending<QueueLane> { it.emulatorPort != null && it.emulatorPort !in matchedPorts }
                .thenByDescending { it.runningCount }
                .thenBy { it.queueName }
        )
        sortedConfigured.forEach { lane ->
            val port = lane.emulatorPort
            val device = port?.let { devicesByPort[it] }
            val matched = port != null && port in matchedPorts
            EmulatorPairRow(lane = lane, device = device, matched = matched)
        }
        if (extraDevices.isNotEmpty()) {
            Text(
                text = "Unbound emulators",
                style = MaterialTheme.typography.labelMedium,
                color = TextSecondary,
                fontWeight = FontWeight.SemiBold,
                modifier = Modifier.padding(top = 4.dp),
            )
            extraDevices.forEach { device ->
                EmulatorPairRow(lane = null, device = device, matched = false)
            }
        }
    }
}

@Composable
private fun EmulatorPairRow(lane: QueueLane?, device: AdbDevice?, matched: Boolean) {
    val accent = when {
        matched -> AccentSuccess
        lane != null -> AccentDanger
        else -> AccentWarning
    }
    Surface(
        shape = RoundedCornerShape(10.dp),
        color = SurfaceElevated,
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .border(1.dp, accent.copy(alpha = 0.28f), RoundedCornerShape(10.dp))
                .padding(horizontal = 10.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Column(
                modifier = Modifier.weight(1f),
                verticalArrangement = Arrangement.spacedBy(1.dp),
            ) {
                if (lane != null) {
                    Text(
                        text = lane.queueName.substringAfterLast('/'),
                        style = MaterialTheme.typography.labelLarge,
                        fontWeight = FontWeight.SemiBold,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    Text(
                        text = "lane · ${lane.runningCount}/${lane.exactCapacity} run · ${lane.waitingCount} wait",
                        style = MaterialTheme.typography.labelSmall,
                        color = TextMuted,
                        maxLines = 1,
                    )
                } else {
                    Text(
                        text = "no configured lane",
                        style = MaterialTheme.typography.labelMedium,
                        color = TextMuted,
                        fontStyle = FontStyle.Italic,
                    )
                }
            }
            Text(
                text = when {
                    matched -> "↔"
                    lane == null -> "→"
                    else -> "✕"
                },
                color = accent,
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
            )
            Column(
                modifier = Modifier.weight(1f),
                horizontalAlignment = Alignment.End,
                verticalArrangement = Arrangement.spacedBy(1.dp),
            ) {
                if (device != null) {
                    Text(
                        text = device.serial,
                        style = MaterialTheme.typography.labelLarge,
                        fontWeight = FontWeight.SemiBold,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    Text(
                        text = device.detailLine.ifBlank { "state ${device.state}" },
                        style = MaterialTheme.typography.labelSmall,
                        color = TextMuted,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                } else {
                    Text(
                        text = "no device on :${lane?.emulatorPort ?: "?"}",
                        style = MaterialTheme.typography.labelMedium,
                        color = AccentDanger,
                        fontWeight = FontWeight.Medium,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    Text(
                        text = "start emulator or remove lane",
                        style = MaterialTheme.typography.labelSmall,
                        color = TextMuted,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
        }
    }
}

// ---------- Environment pane: servers ----------

@Composable
private fun ServersPanel(snapshot: QueueSnapshot) {
    val servers = snapshot.configuration.serverProcesses
    PaneSection(
        title = "Queue Servers",
        subtitle = when {
            servers.isEmpty() -> "No live servers for this data dir"
            servers.size == 1 -> "1 task-queue server"
            else -> "${servers.size} task-queue servers"
        },
    ) {
        if (servers.isEmpty()) {
            Text(
                text = snapshot.configuration.statusMessage
                    ?: "No matching task queue server processes.",
                style = MaterialTheme.typography.bodySmall,
                color = TextMuted,
            )
            return@PaneSection
        }
        servers.forEach { proc ->
            val identity = snapshot.serverIdentityByPid[proc.pid]
            val accentSource = identity?.primaryLabel ?: proc.agentLabel
            val accent = agentColor(accentSource)
            Surface(shape = RoundedCornerShape(10.dp), color = SurfaceElevated) {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .border(1.dp, accent.copy(alpha = 0.2f), RoundedCornerShape(10.dp))
                        .padding(horizontal = 10.dp, vertical = 8.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp),
                ) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(6.dp),
                    ) {
                        Box(
                            Modifier
                                .size(8.dp)
                                .clip(CircleShape)
                                .background(accent),
                        )
                        Text(
                            text = identity?.displayLabel ?: proc.agentLabel,
                            style = MaterialTheme.typography.labelLarge,
                            fontWeight = FontWeight.SemiBold,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                            modifier = Modifier.weight(1f),
                        )
                        Text(
                            text = "pid ${proc.pid}",
                            style = MaterialTheme.typography.labelSmall,
                            color = TextMuted,
                        )
                    }
                    identity?.detailLabel?.let {
                        Text(
                            text = it,
                            style = MaterialTheme.typography.labelSmall,
                            color = TextSecondary,
                        )
                    }
                    if (proc.queueCapacities.isNotEmpty()) {
                        Text(
                            text = proc.queueCapacities.entries.joinToString(" · ") { "${it.key}=${it.value}" },
                            style = MaterialTheme.typography.labelSmall,
                            color = TextMuted,
                            maxLines = 2,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                }
            }
        }
    }
}

// ---------- Shared pane/utility composables ----------

@Composable
private fun PaneSection(
    title: String,
    subtitle: String,
    content: @Composable ColumnScope.() -> Unit,
) {
    Card(
        colors = CardDefaults.cardColors(containerColor = SurfaceCard),
        elevation = CardDefaults.cardElevation(defaultElevation = 1.dp),
    ) {
        Column(
            modifier = Modifier.fillMaxWidth().padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
            content = {
                Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    Text(
                        text = title,
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.SemiBold,
                    )
                    Text(
                        text = subtitle,
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary,
                    )
                }
                content()
            },
        )
    }
}

@Composable
private fun BannerStack(snapshot: QueueSnapshot) {
    val errors = listOfNotNull(
        snapshot.errorMessage,
        snapshot.configuration.errorMessage,
        snapshot.adb.errorMessage,
    )
    if (errors.isEmpty()) return
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        errors.forEach { ErrorBanner(it) }
    }
}

@Composable
private fun EmptyState(title: String, message: String) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = SurfaceCard),
        elevation = CardDefaults.cardElevation(defaultElevation = 1.dp),
    ) {
        Column(
            modifier = Modifier.fillMaxWidth().padding(32.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(
                text = title,
                style = MaterialTheme.typography.titleLarge,
                fontWeight = FontWeight.SemiBold,
                color = TextSecondary,
            )
            Text(
                text = message,
                style = MaterialTheme.typography.bodyMedium,
                color = TextMuted,
                textAlign = TextAlign.Center,
            )
        }
    }
}

@Composable
private fun LegendFooter() {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        LegendDot(color = AccentRunning, label = "running")
        LegendDot(color = AccentWarning, label = "waiting / backup")
        LegendDot(color = AccentDanger, label = "backup + full")
        LegendDot(color = AccentSuccess, label = "emulator matched")
        LegendDot(color = AccentIdle, label = "idle")
    }
}

@Composable
private fun LegendDot(color: Color, label: String) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(5.dp),
    ) {
        Box(
            Modifier
                .size(8.dp)
                .clip(CircleShape)
                .background(color),
        )
        Text(
            text = label,
            style = MaterialTheme.typography.labelSmall,
            color = TextMuted,
        )
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
                .widthIn(max = 320.dp)
                .padding(horizontal = 10.dp, vertical = 8.dp),
            color = Color.White,
            style = MaterialTheme.typography.bodySmall,
        )
    }
}

@Composable
private fun ErrorBanner(message: String) {
    Surface(shape = RoundedCornerShape(14.dp), color = Color(0xFFFBE4E3)) {
        Text(
            text = message,
            modifier = Modifier.fillMaxWidth().padding(14.dp),
            color = MaterialTheme.colorScheme.error,
            style = MaterialTheme.typography.bodyMedium,
        )
    }
}

// ---------- CLI args ----------

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
