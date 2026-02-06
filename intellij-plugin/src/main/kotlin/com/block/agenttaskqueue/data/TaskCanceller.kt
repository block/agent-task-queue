package com.block.agenttaskqueue.data

import com.block.agenttaskqueue.model.QueueTask
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.Service
import com.intellij.openapi.diagnostic.Logger

@Service(Service.Level.APP)
class TaskCanceller {

    companion object {
        private val LOG = Logger.getInstance(TaskCanceller::class.java)

        fun getInstance(): TaskCanceller =
            ApplicationManager.getApplication().getService(TaskCanceller::class.java)
    }

    fun cancelTask(task: QueueTask) {
        ApplicationManager.getApplication().executeOnPooledThread {
            try {
                if (task.status == "running" && task.childPid != null) {
                    killProcessGroup(task.childPid)
                }
                TaskQueueDatabase.getInstance().deleteTask(task.id)
            } catch (e: Exception) {
                LOG.warn("Failed to cancel task #${task.id}", e)
            }
            TaskQueuePoller.getInstance().refreshNow()
        }
    }

    fun clearAllTasks(tasks: List<QueueTask>) {
        ApplicationManager.getApplication().executeOnPooledThread {
            try {
                for (task in tasks) {
                    if (task.status == "running" && task.childPid != null) {
                        killProcessGroup(task.childPid)
                    }
                }
                TaskQueueDatabase.getInstance().deleteAllTasks()
            } catch (e: Exception) {
                LOG.warn("Failed to clear queue", e)
            }
            TaskQueuePoller.getInstance().refreshNow()
        }
    }

    private fun killProcessGroup(pid: Int) {
        // Use negative PID to target the process group (works on both macOS and Linux)
        try {
            ProcessBuilder("kill", "-TERM", "-$pid")
                .redirectErrorStream(true)
                .start()
                .waitFor()
        } catch (e: Exception) {
            LOG.warn("Failed to send SIGTERM to process group -$pid", e)
        }

        try {
            Thread.sleep(500)
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
            return
        }

        try {
            ProcessBuilder("kill", "-9", "-$pid")
                .redirectErrorStream(true)
                .start()
                .waitFor()
        } catch (e: Exception) {
            LOG.debug("SIGKILL to process group -$pid failed (process may already be dead)", e)
        }
    }
}
