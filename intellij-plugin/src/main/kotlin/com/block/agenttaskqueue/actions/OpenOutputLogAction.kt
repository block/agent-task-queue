package com.block.agenttaskqueue.actions

import com.block.agenttaskqueue.settings.TaskQueueSettings
import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.vfs.LocalFileSystem

class OpenOutputLogAction : AnAction() {

    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        e.presentation.isEnabled = e.dataContext.getData(TaskQueueDataKeys.SELECTED_TASK) != null
    }

    override fun actionPerformed(e: AnActionEvent) {
        val task = e.dataContext.getData(TaskQueueDataKeys.SELECTED_TASK) ?: return
        val project = e.project ?: return

        val path = "${TaskQueueSettings.getInstance().outputDir}/task_${task.id}.log"
        val file = LocalFileSystem.getInstance().findFileByPath(path)

        if (file != null) {
            FileEditorManager.getInstance(project).openFile(file, true)
        } else {
            Messages.showInfoMessage(
                project,
                "Output log not found: $path",
                "Log Not Found"
            )
        }
    }
}
