package com.block.agenttaskqueue.ui

import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowFactory
import com.intellij.ui.content.ContentFactory

class TaskQueueToolWindowFactory : ToolWindowFactory {

    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        val contentFactory = ContentFactory.getInstance()

        val queuePanel = TaskQueuePanel(project)
        val queueContent = contentFactory.createContent(queuePanel, "Queue", false)
        toolWindow.contentManager.addContent(queueContent)

        val outputPanel = OutputPanel(project)
        val outputContent = contentFactory.createContent(outputPanel, "Output", false)
        toolWindow.contentManager.addContent(outputContent)
    }
}
