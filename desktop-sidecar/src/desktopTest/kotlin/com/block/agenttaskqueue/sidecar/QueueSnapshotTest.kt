package com.block.agenttaskqueue.sidecar

import java.time.Instant
import java.nio.file.Paths
import java.util.TimeZone
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class QueueSnapshotTest {
    @Test
    fun runningTasksInterpretUpdatedAtInLocalTime() {
        withDefaultTimeZone("America/Los_Angeles") {
            val task = QueueTask(
                id = 1,
                queueName = "global",
                status = "running",
                command = null,
                pid = null,
                childPid = null,
                createdAt = "2026-04-22 19:00:00",
                updatedAt = "2026-04-22T12:00:00",
            )

            assertEquals("running 15m", task.statusAge(Instant.parse("2026-04-22T19:15:00Z")))
        }
    }

    @Test
    fun waitingTasksKeepCreatedAtOnUtcTimeline() {
        withDefaultTimeZone("America/Los_Angeles") {
            val task = QueueTask(
                id = 1,
                queueName = "global",
                status = "waiting",
                command = null,
                pid = null,
                childPid = null,
                createdAt = "2026-04-22 19:00:00",
                updatedAt = null,
            )

            assertEquals("queued 15m", task.statusAge(Instant.parse("2026-04-22T19:15:00Z")))
        }
    }

    @Test
    fun configuredQueuesStayVisibleWhenIdle() {
        val snapshot = QueueSnapshot.fromTasks(
            dataDir = Paths.get("/tmp/agent-task-queue"),
            tasks = emptyList(),
            configuration = QueueConfigurationSnapshot(
                serverProcesses = emptyList(),
                configuredScopes = listOf(
                    ConfiguredQueueScope(
                        scopeName = "gradle",
                        capacities = setOf(2),
                        sourcePids = listOf(1234),
                    ),
                    ConfiguredQueueScope(
                        scopeName = "gradle/emulator-5554",
                        capacities = setOf(1),
                        sourcePids = listOf(1234),
                    ),
                ),
            ),
        )

        assertEquals(listOf("gradle", "gradle/emulator-5554"), snapshot.queueLanes.map { it.queueName })
        assertEquals(1, snapshot.scopeGroups.size)
        assertEquals(listOf(2, 1), snapshot.configuredScopeUsage.mapNotNull { it.capacity })
        assertTrue(snapshot.queueLanes.all { it.tasks.isEmpty() })
    }

    @Test
    fun taskIdentityPrefersAgentRepoAndBranchMetadata() {
        val task = QueueTask(
            id = 7,
            queueName = "gradle/emulator-5554",
            status = "running",
            command = "./gradlew connectedDebugAndroidTest",
            pid = 902,
            childPid = 8112,
            createdAt = null,
            updatedAt = null,
            workingDirectory = "/Users/example/Development/sample-repo",
            worktreeRoot = "/Users/example/Development/sample-repo-worktrees/feature-queue-view",
            repoName = "sample-repo",
            gitBranch = "feature/queue-view",
            agentName = "amp",
        )

        assertEquals("Amp", task.displayAgentLabel)
        assertEquals("sample-repo · feature/queue-view", task.displayContextLabel)
        assertEquals("Amp · sample-repo · feature/queue-view", task.displayIdentityLabel)
    }

    @Test
    fun serverIdentityUsesVisibleTaskContextInsteadOfLaunchBranch() {
        val snapshot = QueueSnapshot.fromTasks(
            dataDir = Paths.get("/tmp/agent-task-queue"),
            tasks = listOf(
                QueueTask(
                    id = 7,
                    queueName = "gradle",
                    status = "running",
                    command = "./gradlew test",
                    pid = 902,
                    childPid = null,
                    createdAt = null,
                    updatedAt = null,
                    repoName = "sample-mobile-app",
                    gitBranch = "feature/real-work",
                    agentName = "amp",
                )
            ),
            configuration = QueueConfigurationSnapshot(
                serverProcesses = listOf(
                    QueueServerProcess(
                        pid = 902,
                        commandLine = "python task_queue.py",
                        dataDir = Paths.get("/tmp/agent-task-queue"),
                        queueCapacities = emptyMap(),
                        agentLabel = "Amp deep",
                        contextLabel = "desktop-sidecar",
                    )
                ),
                configuredScopes = emptyList(),
            ),
        )

        val identity = snapshot.serverIdentityByPid.getValue(902)
        assertEquals("Amp", identity.primaryLabel)
        assertEquals("sample-mobile-app · feature/real-work", identity.contextLabel)
        assertEquals("desktop-sidecar", identity.launchContextLabel)
    }

    @Test
    fun idleServerDoesNotPretendLaunchBranchIsQueueUsage() {
        val snapshot = QueueSnapshot.fromTasks(
            dataDir = Paths.get("/tmp/agent-task-queue"),
            tasks = emptyList(),
            configuration = QueueConfigurationSnapshot(
                serverProcesses = listOf(
                    QueueServerProcess(
                        pid = 902,
                        commandLine = "python task_queue.py",
                        dataDir = Paths.get("/tmp/agent-task-queue"),
                        queueCapacities = emptyMap(),
                        agentLabel = "Amp deep",
                        contextLabel = "desktop-sidecar",
                    )
                ),
                configuredScopes = emptyList(),
            ),
        )

        val identity = snapshot.serverIdentityByPid.getValue(902)
        assertEquals("Amp deep", identity.displayLabel)
        assertEquals(null, identity.contextLabel)
        assertEquals("desktop-sidecar", identity.launchContextLabel)
        assertEquals("idle server", identity.detailLabel)
    }

    @Test
    fun idleServerFallsBackToHistoricalMetricsUsage() {
        val snapshot = QueueSnapshot.fromTasks(
            dataDir = Paths.get("/tmp/agent-task-queue"),
            tasks = emptyList(),
            configuration = QueueConfigurationSnapshot(
                serverProcesses = listOf(
                    QueueServerProcess(
                        pid = 902,
                        commandLine = "python task_queue.py",
                        dataDir = Paths.get("/tmp/agent-task-queue"),
                        queueCapacities = emptyMap(),
                        agentLabel = "Amp deep",
                        contextLabel = "desktop-sidecar",
                    )
                ),
                configuredScopes = emptyList(),
            ),
            metrics = QueueMetricsSnapshot(
                latestUsageByPid = mapOf(
                    902 to HistoricalTaskUsage(
                        pid = 902,
                        timestamp = "2026-04-24T11:10:13.561185",
                        repoName = "sample-mobile-app",
                        gitBranch = "feature/real-work",
                        agentName = "amp",
                    )
                )
            ),
        )

        val identity = snapshot.serverIdentityByPid.getValue(902)
        assertEquals("Amp · sample-mobile-app · feature/real-work", identity.displayLabel)
        assertEquals("desktop-sidecar", identity.launchContextLabel)
        assertEquals("idle server", identity.detailLabel)
    }

    private fun withDefaultTimeZone(timeZoneId: String, block: () -> Unit) {
        val original = TimeZone.getDefault()
        try {
            TimeZone.setDefault(TimeZone.getTimeZone(timeZoneId))
            block()
        } finally {
            TimeZone.setDefault(original)
        }
    }
}
