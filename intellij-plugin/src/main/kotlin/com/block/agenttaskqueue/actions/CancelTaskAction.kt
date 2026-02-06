package com.block.agenttaskqueue.actions

import com.block.agenttaskqueue.data.TaskCanceller
import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.ui.Messages

class CancelTaskAction : AnAction() {

    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        e.presentation.isEnabled = e.dataContext.getData(TaskQueueDataKeys.SELECTED_TASK) != null
    }

    override fun actionPerformed(e: AnActionEvent) {
        val task = e.dataContext.getData(TaskQueueDataKeys.SELECTED_TASK) ?: return
        val project = e.project
        val result = Messages.showYesNoDialog(
            project,
            "Cancel task #${task.id}?",
            "Cancel Task",
            Messages.getQuestionIcon()
        )
        if (result == Messages.YES) {
            TaskCanceller.getInstance().cancelTask(task)
        }
    }
}
