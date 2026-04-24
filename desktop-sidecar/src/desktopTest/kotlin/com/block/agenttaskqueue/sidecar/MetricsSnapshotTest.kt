package com.block.agenttaskqueue.sidecar

import kotlin.test.Test
import kotlin.test.assertEquals

class MetricsSnapshotTest {
    @Test
    fun parseMetricsSnapshotKeepsLatestUsagePerPid() {
        val snapshot = parseMetricsSnapshot(
            """
            {"event":"task_queued","timestamp":"2026-04-24T11:00:01.389560","task_id":74,"queue_name":"global","pid":88226,"agent_name":"amp","repo_name":"agent-task-queue","git_branch":"sedwards/no-ticket/queue-visibility"}
            {"event":"task_completed","timestamp":"2026-04-24T11:10:13.561185","task_id":77,"queue_name":"global","pid":88226,"agent_name":"amp","repo_name":"android-register","git_branch":"sedwards/no-ticket/real-work"}
            {"event":"task_completed","timestamp":"2026-04-24T11:10:33.478135","task_id":78,"queue_name":"gradle","pid":74067,"agent_name":"claude","repo_name":"cash-android","git_branch":"feature/payments"}
            """.trimIndent()
        )

        assertEquals(2, snapshot.latestUsageByPid.size)
        assertEquals(
            "android-register · sedwards/no-ticket/real-work",
            snapshot.latestUsageByPid.getValue(88226).displayContextLabel,
        )
        assertEquals("Amp", snapshot.latestUsageByPid.getValue(88226).displayAgentLabel)
        assertEquals("Claude", snapshot.latestUsageByPid.getValue(74067).displayAgentLabel)
    }
}
