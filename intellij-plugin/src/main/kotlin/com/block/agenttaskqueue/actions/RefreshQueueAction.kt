package com.block.agenttaskqueue.actions

import com.block.agenttaskqueue.data.TaskQueuePoller
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent

class RefreshQueueAction : AnAction() {

    override fun actionPerformed(e: AnActionEvent) {
        TaskQueuePoller.getInstance().refreshNow()
    }
}
