package com.block.agenttaskqueue.ui

import com.block.agenttaskqueue.model.QueueSummary
import com.block.agenttaskqueue.model.QueueTask
import com.block.agenttaskqueue.model.TaskQueueListener
import com.block.agenttaskqueue.model.TaskQueueModel
import com.block.agenttaskqueue.settings.TaskQueueSettings
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.Disposer
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowFactory
import com.intellij.ui.content.ContentFactory
import com.intellij.ui.content.ContentManagerEvent
import com.intellij.ui.content.ContentManagerListener

class TaskQueueToolWindowFactory : ToolWindowFactory {

    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        val contentFactory = ContentFactory.getInstance()
        val contentManager = toolWindow.contentManager

        // Queue tab (not closeable)
        val queuePanel = TaskQueuePanel(project)
        val queueContent = contentFactory.createContent(queuePanel, "Queue", false)
        queueContent.isCloseable = false
        contentManager.addContent(queueContent)

        // Track which tasks already have output tabs
        val taskTabIds = mutableSetOf<Int>()

        // Remove task ID from tracking when a tab is closed so it can be reopened
        contentManager.addContentManagerListener(object : ContentManagerListener {
            override fun contentRemoved(event: ContentManagerEvent) {
                val panel = event.content.component as? OutputPanel ?: return
                taskTabIds.remove(panel.taskId)
            }
        })

        fun openOutputTab(task: QueueTask) {
            // If tab already exists, select it
            for (content in contentManager.contents) {
                val panel = content.component as? OutputPanel ?: continue
                if (panel.taskId == task.id) {
                    contentManager.setSelectedContent(content)
                    return
                }
            }

            taskTabIds.add(task.id)
            val tabTitle = task.displayCommand.take(30) +
                if (task.displayCommand.length > 30) "..." else ""
            val outputDir = TaskQueueSettings.getInstance().outputDir
            val logPath = "$outputDir/task_${task.id}.raw.log"

            val outputPanel = OutputPanel(project, task.id, logPath)
            Disposer.register(contentManager, outputPanel)
            val content = contentFactory.createContent(outputPanel, tabTitle, false)
            content.isCloseable = true
            content.setDisposer(outputPanel)
            content.description = task.displayCommand
            contentManager.addContent(content)
            contentManager.setSelectedContent(content)
        }

        // Auto-open output tab when a task starts running
        project.messageBus.connect(contentManager).subscribe(TaskQueueModel.TOPIC, object : TaskQueueListener {
            override fun onQueueUpdated(tasks: List<QueueTask>, summary: QueueSummary) {
                val runningTask = tasks.firstOrNull { it.status == "running" } ?: return
                if (runningTask.id in taskTabIds) return
                openOutputTab(runningTask)
            }
        })

        // Click on a task in the queue table to open its output tab
        queuePanel.onTaskClicked = { task -> openOutputTab(task) }
    }
}
