package com.block.agenttaskqueue.sidecar

import java.time.Instant
import java.util.TimeZone
import kotlin.test.Test
import kotlin.test.assertEquals

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
