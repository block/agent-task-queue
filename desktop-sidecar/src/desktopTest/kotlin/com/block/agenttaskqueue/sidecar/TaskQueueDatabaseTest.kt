package com.block.agenttaskqueue.sidecar

import java.nio.file.Files
import java.sql.DriverManager
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class TaskQueueDatabaseTest {
    @Test
    fun missingQueueTableIsTreatedAsWaitingForSchema() {
        val tempDir = Files.createTempDirectory("task-queue-db-test")
        try {
            val dbPath = tempDir.resolve("queue.db")
            DriverManager.getConnection("jdbc:sqlite:$dbPath").use { connection ->
                connection.createStatement().use { statement ->
                    statement.execute("CREATE TABLE metadata (id INTEGER PRIMARY KEY)")
                }
            }

            val snapshot = TaskQueueDatabase.loadSnapshot(tempDir)

            assertEquals(emptyList(), snapshot.tasks)
            assertEquals("Waiting for queue schema at $dbPath", snapshot.statusMessage)
            assertNull(snapshot.errorMessage)
        } finally {
            tempDir.toFile().deleteRecursively()
        }
    }

    @Test
    fun coerceNullableIntKeepsInRangeValues() {
        assertEquals(42, coerceNullableInt(42L))
        assertEquals(7, coerceNullableInt("7"))
    }

    @Test
    fun coerceNullableIntRejectsOutOfRangeValues() {
        assertNull(coerceNullableInt(Long.MAX_VALUE))
        assertNull(coerceNullableInt("2147483648"))
    }
}
