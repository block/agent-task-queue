package com.block.agenttaskqueue.ui

import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.StatusBarWidget
import com.intellij.openapi.wm.StatusBarWidgetFactory

class TaskQueueStatusBarWidgetFactory : StatusBarWidgetFactory {

    override fun getId(): String = "AgentTaskQueueStatusBar"

    override fun getDisplayName(): String = "Agent Task Queue"

    override fun createWidget(project: Project): StatusBarWidget = TaskQueueStatusBarWidget(project)
}
