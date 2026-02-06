package com.block.agenttaskqueue.ui

import com.block.agenttaskqueue.TaskQueueIcons
import com.block.agenttaskqueue.data.TaskQueuePoller
import com.block.agenttaskqueue.model.QueueSummary
import com.block.agenttaskqueue.model.QueueTask
import com.block.agenttaskqueue.model.TaskQueueListener
import com.block.agenttaskqueue.model.TaskQueueModel
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.CustomStatusBarWidget
import com.intellij.openapi.wm.StatusBar
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.ui.components.JBLabel
import com.intellij.util.ui.JBUI
import java.awt.event.MouseAdapter
import java.awt.event.MouseEvent
import javax.swing.JComponent

class TaskQueueStatusBarWidget(private val project: Project) : CustomStatusBarWidget {

    private var myStatusBar: StatusBar? = null
    private val label = JBLabel().apply {
        icon = TaskQueueIcons.TaskQueue
        border = JBUI.Borders.empty(0, 4)
        toolTipText = "Agent Task Queue - Click to open"
        addMouseListener(object : MouseAdapter() {
            override fun mouseClicked(e: MouseEvent) {
                ToolWindowManager.getInstance(project).getToolWindow("Agent Task Queue")?.activate(null)
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
        val model = TaskQueueModel.getInstance()
        val tasks = model.tasks
        val summary = model.summary

        label.text = when {
            tasks.isEmpty() -> "Task Queue: empty"
            else -> {
                val runningTask = tasks.firstOrNull { it.status == "running" }
                if (runningTask != null) {
                    val cmd = runningTask.command ?: "unknown"
                    val truncatedCmd = cmd.take(40) + if (cmd.length > 40) "..." else ""
                    if (summary.waiting > 0) "Task Queue: $truncatedCmd (+${summary.waiting})"
                    else "Task Queue: $truncatedCmd"
                } else {
                    "Task Queue: waiting (${summary.waiting})"
                }
            }
        }
    }
}
