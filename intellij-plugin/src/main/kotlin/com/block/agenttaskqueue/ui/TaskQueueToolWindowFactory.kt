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

        project.messageBus.connect(contentManager).subscribe(TaskQueueModel.TOPIC, object : TaskQueueListener {
            override fun onQueueUpdated(tasks: List<QueueTask>, summary: QueueSummary) {
                val runningTask = tasks.firstOrNull { it.status == "running" } ?: return
                if (runningTask.id in taskTabIds) return
                taskTabIds.add(runningTask.id)

                val tabTitle = runningTask.displayCommand.take(30) +
                    if (runningTask.displayCommand.length > 30) "..." else ""
                val outputDir = TaskQueueSettings.getInstance().outputDir
                val logPath = "$outputDir/task_${runningTask.id}.raw.log"

                val outputPanel = OutputPanel(project, runningTask.id, logPath)
                Disposer.register(contentManager, outputPanel)
                val content = contentFactory.createContent(outputPanel, tabTitle, false)
                content.isCloseable = true
                content.setDisposer(outputPanel)
                content.description = runningTask.displayCommand
                contentManager.addContent(content)
                contentManager.setSelectedContent(content)
            }
        })
    }
}
