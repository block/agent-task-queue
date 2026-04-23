package com.block.agenttaskqueue.sidecar

import java.nio.file.Path
import java.sql.DriverManager
import java.sql.ResultSet

object TaskQueueDatabase {
    init {
        Class.forName("org.sqlite.JDBC")
    }

    fun loadSnapshot(dataDir: Path): QueueSnapshot {
        val dbPath = dataDir.resolve("queue.db")
        if (!dbPath.toFile().exists()) {
            return QueueSnapshot.empty(
                dataDir = dataDir,
                statusMessage = "Waiting for queue database at $dbPath",
            )
        }

        return runCatching {
            DriverManager.getConnection("jdbc:sqlite:$dbPath").use { connection ->
                connection.createStatement().use { statement ->
                    statement.execute("PRAGMA journal_mode=WAL")
                    statement.execute("PRAGMA busy_timeout=5000")
                }

                connection.createStatement().use { statement ->
                    statement.executeQuery("SELECT * FROM queue ORDER BY queue_name, id").use { rs ->
                        val tasks = mutableListOf<QueueTask>()
                        while (rs.next()) {
                            tasks += QueueTask(
                                id = rs.getInt("id"),
                                queueName = rs.getString("queue_name"),
                                status = rs.getString("status"),
                                command = rs.getString("command"),
                                pid = rs.getNullableInt("pid"),
                                childPid = rs.getNullableInt("child_pid"),
                                createdAt = rs.getString("created_at"),
                                updatedAt = rs.getString("updated_at"),
                            )
                        }

                        QueueSnapshot.fromTasks(
                            dataDir = dataDir,
                            tasks = tasks,
                            statusMessage = if (tasks.isEmpty()) "Queue is empty" else null,
                        )
                    }
                }
            }
        }.getOrElse { error ->
            QueueSnapshot.empty(
                dataDir = dataDir,
                errorMessage = error.message ?: "Failed to read $dbPath",
            )
        }
    }
}

private fun ResultSet.getNullableInt(columnName: String): Int? {
    val value = getObject(columnName) ?: return null
    return when (value) {
        is Int -> value
        is Long -> value.toInt()
        is Number -> value.toInt()
        else -> value.toString().toIntOrNull()
    }
}
