package com.block.agenttaskqueue.actions

import com.block.agenttaskqueue.data.TaskCanceller
import com.block.agenttaskqueue.model.TaskQueueModel
import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.ui.Messages

class ClearQueueAction : AnAction() {

    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        e.presentation.isEnabled = TaskQueueModel.getInstance().tasks.isNotEmpty()
    }

    override fun actionPerformed(e: AnActionEvent) {
        val tasks = TaskQueueModel.getInstance().tasks
        if (tasks.isEmpty()) return

        val runningCount = tasks.count { it.status == "running" }
        val message = "Clear all ${tasks.size} tasks? $runningCount running task(s) will be killed."
        val result = Messages.showYesNoDialog(
            e.project,
            message,
            "Clear Queue",
            Messages.getWarningIcon()
        )
        if (result == Messages.YES) {
            TaskCanceller.getInstance().clearAllTasks(tasks)
        }
    }
}
