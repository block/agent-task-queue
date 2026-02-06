package com.block.agenttaskqueue.model

data class QueueSummary(
    val total: Int,
    val running: Int,
    val waiting: Int,
) {
    companion object {
        val EMPTY = QueueSummary(total = 0, running = 0, waiting = 0)

        fun fromTasks(tasks: List<QueueTask>): QueueSummary {
            return QueueSummary(
                total = tasks.size,
                running = tasks.count { it.status == "running" },
                waiting = tasks.count { it.status == "waiting" },
            )
        }
    }
}
