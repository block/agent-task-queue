package com.block.agenttaskqueue.actions

import com.block.agenttaskqueue.model.QueueTask
import com.intellij.openapi.actionSystem.DataKey

object TaskQueueDataKeys {
    val SELECTED_TASK: DataKey<QueueTask> = DataKey.create("AgentTaskQueue.SelectedTask")
}
