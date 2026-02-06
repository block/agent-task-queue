package com.block.agenttaskqueue.data

import com.block.agenttaskqueue.model.TaskQueueModel
import com.intellij.openapi.Disposable
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.Service
import com.intellij.openapi.diagnostic.Logger
import com.intellij.openapi.util.Disposer
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

@Service(Service.Level.APP)
class TaskQueuePoller : Disposable {

    companion object {
        private val LOG = Logger.getInstance(TaskQueuePoller::class.java)
        private const val ACTIVE_INTERVAL_MS = 1000L
        private const val IDLE_INTERVAL_MS = 3000L

        fun getInstance(): TaskQueuePoller =
            ApplicationManager.getApplication().getService(TaskQueuePoller::class.java)
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val refreshChannel = Channel<Unit>(Channel.CONFLATED)
    private var previousTasks = emptyList<com.block.agenttaskqueue.model.QueueTask>()

    init {
        Disposer.register(ApplicationManager.getApplication(), this)
        scope.launch {
            while (true) {
                poll()
                val interval = if (previousTasks.isNotEmpty()) ACTIVE_INTERVAL_MS else IDLE_INTERVAL_MS
                // Wait for the interval, but wake up early if refreshNow() is called
                withTimeoutOrNull(interval) {
                    refreshChannel.receive()
                }
            }
        }
    }

    private fun poll() {
        try {
            val tasks = TaskQueueDatabase.getInstance().fetchAllTasks()
            if (tasks != previousTasks) {
                previousTasks = tasks
                TaskQueueModel.getInstance().update(tasks)
            }
        } catch (e: Exception) {
            LOG.warn("Failed to poll task queue", e)
        }
    }

    fun refreshNow() {
        refreshChannel.trySend(Unit)
    }

    override fun dispose() {
        scope.coroutineContext[kotlinx.coroutines.Job.Key]?.cancel()
    }
}
