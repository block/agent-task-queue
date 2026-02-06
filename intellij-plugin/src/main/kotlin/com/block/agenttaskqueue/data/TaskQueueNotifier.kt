package com.block.agenttaskqueue.data

import com.block.agenttaskqueue.model.QueueSummary
import com.block.agenttaskqueue.model.QueueTask
import com.block.agenttaskqueue.model.TaskQueueListener
import com.block.agenttaskqueue.model.TaskQueueModel
import com.block.agenttaskqueue.settings.TaskQueueSettings
import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.Service
import com.intellij.openapi.diagnostic.Logger
import java.io.File

@Service(Service.Level.APP)
class TaskQueueNotifier {

    companion object {
        private val LOG = Logger.getInstance(TaskQueueNotifier::class.java)
        private const val NOTIFICATION_GROUP_ID = "Agent Task Queue"

        fun getInstance(): TaskQueueNotifier =
            ApplicationManager.getApplication().getService(TaskQueueNotifier::class.java)
    }

    private var previousTaskIds = emptySet<Int>()
    private var previousRunningIds = emptySet<Int>()
    private var previousCommands = emptyMap<Int, String>()

    init {
        ApplicationManager.getApplication().messageBus.connect()
            .subscribe(TaskQueueModel.TOPIC, object : TaskQueueListener {
                override fun onQueueUpdated(tasks: List<QueueTask>, summary: QueueSummary) {
                    processUpdate(tasks)
                }
            })
    }

    private fun processUpdate(tasks: List<QueueTask>) {
        if (!TaskQueueSettings.getInstance().notificationsEnabled) {
            updateTracking(tasks)
            return
        }

        val currentTaskIds = tasks.map { it.id }.toSet()
        val currentRunningIds = tasks.filter { it.status == "running" }.map { it.id }.toSet()
        val currentCommands = tasks.associate { it.id to (it.command ?: "unknown") }

        // Detect newly running tasks
        val newlyRunning = currentRunningIds - previousRunningIds
        for (id in newlyRunning) {
            // Only notify if this task existed before (was waiting) or is brand new
            val cmd = currentCommands[id] ?: "unknown"
            notify("Running: $cmd", NotificationType.INFORMATION)
        }

        // Detect disappeared tasks (finished)
        val disappeared = previousTaskIds - currentTaskIds
        for (id in disappeared) {
            val cmd = previousCommands[id] ?: "unknown"
            // Only notify for tasks that were running (not waiting tasks that got cancelled)
            if (id in previousRunningIds) {
                val exitCode = readExitCode(id)
                if (exitCode != null && exitCode != 0) {
                    notifyWithAction("Failed: $cmd (exit code $exitCode)", NotificationType.ERROR, id)
                } else {
                    notify("Finished: $cmd", NotificationType.INFORMATION)
                }
            }
        }

        updateTracking(tasks)
    }

    private fun updateTracking(tasks: List<QueueTask>) {
        previousTaskIds = tasks.map { it.id }.toSet()
        previousRunningIds = tasks.filter { it.status == "running" }.map { it.id }.toSet()
        previousCommands = tasks.associate { it.id to (it.command ?: "unknown") }
    }

    private fun readExitCode(taskId: Int): Int? {
        return try {
            val outputDir = TaskQueueSettings.getInstance().outputDir
            val logFile = File("$outputDir/task_$taskId.log")
            if (!logFile.exists()) return null
            val content = logFile.readText()
            val match = Regex("""EXIT CODE:\s*(\d+)""").find(content)
            match?.groupValues?.get(1)?.toIntOrNull()
        } catch (e: Exception) {
            LOG.debug("Failed to read exit code for task $taskId", e)
            null
        }
    }

    private fun notify(content: String, type: NotificationType) {
        NotificationGroupManager.getInstance()
            .getNotificationGroup(NOTIFICATION_GROUP_ID)
            .createNotification(content, type)
            .notify(null)
    }

    private fun notifyWithAction(content: String, type: NotificationType, taskId: Int) {
        val outputDir = TaskQueueSettings.getInstance().outputDir
        val logPath = "$outputDir/task_$taskId.log"

        val notification = NotificationGroupManager.getInstance()
            .getNotificationGroup(NOTIFICATION_GROUP_ID)
            .createNotification(content, type)
            .setImportant(true)

        notification.addAction(object : com.intellij.notification.NotificationAction("View Output") {
            override fun actionPerformed(
                e: com.intellij.openapi.actionSystem.AnActionEvent,
                notification: com.intellij.notification.Notification
            ) {
                val project = e.project ?: return
                val vf = com.intellij.openapi.vfs.LocalFileSystem.getInstance().findFileByPath(logPath)
                if (vf != null) {
                    com.intellij.openapi.fileEditor.FileEditorManager.getInstance(project).openFile(vf, true)
                }
                notification.expire()
            }
        })

        notification.notify(null)
    }
}
