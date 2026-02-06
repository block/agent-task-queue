package com.block.agenttaskqueue.model

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.Service
import com.intellij.util.messages.Topic

interface TaskQueueListener {
    fun onQueueUpdated(tasks: List<QueueTask>, summary: QueueSummary)
}

@Service(Service.Level.APP)
class TaskQueueModel {

    companion object {
        val TOPIC = Topic.create("AgentTaskQueue.Update", TaskQueueListener::class.java)

        fun getInstance(): TaskQueueModel =
            ApplicationManager.getApplication().getService(TaskQueueModel::class.java)
    }

    @Volatile
    var tasks: List<QueueTask> = emptyList()
        private set

    @Volatile
    var summary: QueueSummary = QueueSummary.EMPTY
        private set

    fun update(newTasks: List<QueueTask>) {
        tasks = newTasks
        summary = QueueSummary.fromTasks(newTasks)
        ApplicationManager.getApplication().invokeLater {
            ApplicationManager.getApplication().messageBus
                .syncPublisher(TOPIC)
                .onQueueUpdated(tasks, summary)
        }
    }
}
