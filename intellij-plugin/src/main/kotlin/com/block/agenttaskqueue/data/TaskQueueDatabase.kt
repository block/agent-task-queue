package com.block.agenttaskqueue.data

import com.block.agenttaskqueue.model.QueueTask
import com.block.agenttaskqueue.settings.TaskQueueSettings
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.Service
import com.intellij.openapi.diagnostic.Logger
import java.io.File
import java.sql.Connection
import java.sql.DriverManager

@Service(Service.Level.APP)
class TaskQueueDatabase {

    companion object {
        private val LOG = Logger.getInstance(TaskQueueDatabase::class.java)

        init {
            try {
                Class.forName("org.sqlite.JDBC")
            } catch (e: ClassNotFoundException) {
                LOG.error("SQLite JDBC driver not found", e)
            }
        }

        fun getInstance(): TaskQueueDatabase =
            ApplicationManager.getApplication().getService(TaskQueueDatabase::class.java)
    }

    private fun getConnection(): Connection? {
        val dbPath = TaskQueueSettings.getInstance().dbPath
        if (!File(dbPath).exists()) {
            LOG.debug("Database file not found: $dbPath")
            return null
        }

        val conn = DriverManager.getConnection("jdbc:sqlite:$dbPath")
        conn.createStatement().execute("PRAGMA journal_mode=WAL")
        conn.createStatement().execute("PRAGMA busy_timeout=5000")
        return conn
    }

    fun fetchAllTasks(): List<QueueTask> {
        val conn = getConnection() ?: return emptyList()
        return conn.use { c ->
            val stmt = c.createStatement()
            val rs = stmt.executeQuery("SELECT * FROM queue ORDER BY queue_name, id")
            val tasks = mutableListOf<QueueTask>()
            while (rs.next()) {
                tasks.add(
                    QueueTask(
                        id = rs.getInt("id"),
                        queueName = rs.getString("queue_name"),
                        status = rs.getString("status"),
                        command = rs.getString("command"),
                        pid = rs.getObject("pid") as? Int,
                        childPid = rs.getObject("child_pid") as? Int,
                        createdAt = rs.getString("created_at"),
                        updatedAt = rs.getString("updated_at"),
                    )
                )
            }
            tasks
        }
    }

    fun deleteTask(id: Int) {
        val conn = getConnection() ?: return
        conn.use { c ->
            val stmt = c.prepareStatement("DELETE FROM queue WHERE id = ?")
            stmt.setInt(1, id)
            stmt.executeUpdate()
        }
    }

    fun deleteAllTasks() {
        val conn = getConnection() ?: return
        conn.use { c ->
            c.createStatement().executeUpdate("DELETE FROM queue")
        }
    }
}
