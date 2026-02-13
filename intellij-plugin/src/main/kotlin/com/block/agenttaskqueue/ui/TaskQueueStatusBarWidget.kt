package com.block.agenttaskqueue.ui

import com.block.agenttaskqueue.TaskQueueIcons
import com.block.agenttaskqueue.data.TaskQueuePoller
import com.block.agenttaskqueue.model.QueueSummary
import com.block.agenttaskqueue.model.QueueTask
import com.block.agenttaskqueue.model.TaskQueueListener
import com.block.agenttaskqueue.model.TaskQueueModel
import com.block.agenttaskqueue.settings.TaskQueueSettings
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.CustomStatusBarWidget
import com.intellij.openapi.wm.StatusBar
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.ui.components.JBLabel
import com.intellij.util.ui.JBUI
import java.awt.event.MouseAdapter
import java.awt.event.MouseEvent
import java.time.Duration
import java.time.Instant
import java.time.LocalDateTime
import java.time.ZoneOffset
import java.time.format.DateTimeParseException
import javax.swing.JComponent

class TaskQueueStatusBarWidget(private val project: Project) : CustomStatusBarWidget {

    private var myStatusBar: StatusBar? = null
    private val label = JBLabel().apply {
        icon = TaskQueueIcons.TaskQueue
        border = JBUI.Borders.empty(0, 4)
        toolTipText = "Agent Task Queue - Click to open"
        addMouseListener(object : MouseAdapter() {
            override fun mouseClicked(e: MouseEvent) {
                val toolWindow = ToolWindowManager.getInstance(project).getToolWindow("Agent Task Queue") ?: return
                if (toolWindow.isVisible) toolWindow.hide() else toolWindow.activate(null)
            }
        })
    }

    override fun ID(): String = "AgentTaskQueueStatusBar"

    override fun install(statusBar: StatusBar) {
        myStatusBar = statusBar
        project.messageBus.connect().subscribe(TaskQueueModel.TOPIC, object : TaskQueueListener {
            override fun onQueueUpdated(tasks: List<QueueTask>, summary: QueueSummary) {
                updateLabel()
                myStatusBar?.updateWidget(ID())
            }
        })
        TaskQueuePoller.getInstance()
        updateLabel()
    }

    override fun dispose() {}

    override fun getComponent(): JComponent = label

    private fun updateLabel() {
        val mode = TaskQueueSettings.getInstance().displayMode
        val model = TaskQueueModel.getInstance()
        val tasks = model.tasks
        val summary = model.summary

        when (mode) {
            "hidden" -> {
                label.isVisible = false
                return
            }
            "minimal" -> {
                label.isVisible = true
                label.text = ""
                label.icon = TaskQueueIcons.TaskQueue
                return
            }
        }

        label.isVisible = true
        label.icon = TaskQueueIcons.TaskQueue

        label.text = when {
            tasks.isEmpty() -> "Task Queue: empty"
            else -> {
                val runningTask = tasks.firstOrNull { it.status == "running" }
                if (runningTask != null) {
                    val cmd = runningTask.displayCommand
                    val truncatedCmd = cmd.take(40) + if (cmd.length > 40) "..." else ""
                    when (mode) {
                        "verbose" -> {
                            val elapsed = formatElapsed(runningTask.updatedAt)
                            val waitSuffix = if (summary.waiting > 0) " (+${summary.waiting} waiting)" else ""
                            "Task Queue: $truncatedCmd [$elapsed]$waitSuffix"
                        }
                        else -> {
                            if (summary.waiting > 0) "Task Queue: $truncatedCmd (+${summary.waiting})"
                            else "Task Queue: $truncatedCmd"
                        }
                    }
                } else {
                    "Task Queue: waiting (${summary.waiting})"
                }
            }
        }
    }

    private fun formatElapsed(timestamp: String?): String {
        if (timestamp == null) return "0s"
        return try {
            val ts = timestamp.replace(" ", "T")
            val parsed = LocalDateTime.parse(ts)
            val instant = parsed.toInstant(ZoneOffset.UTC)
            val duration = Duration.between(instant, Instant.now())
            val totalSeconds = duration.seconds
            when {
                totalSeconds < 60 -> "${totalSeconds}s"
                totalSeconds < 3600 -> "${totalSeconds / 60}m ${totalSeconds % 60}s"
                else -> "${totalSeconds / 3600}h ${(totalSeconds % 3600) / 60}m"
            }
        } catch (_: DateTimeParseException) {
            "0s"
        }
    }
}
