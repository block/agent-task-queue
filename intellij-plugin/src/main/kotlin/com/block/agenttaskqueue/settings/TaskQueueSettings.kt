package com.block.agenttaskqueue.settings

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.PersistentStateComponent
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage

@Service(Service.Level.APP)
@State(name = "AgentTaskQueueSettings", storages = [Storage("AgentTaskQueueSettings.xml")])
class TaskQueueSettings : PersistentStateComponent<TaskQueueSettings.State> {

    data class State(
        var dataDir: String = System.getenv("TASK_QUEUE_DATA_DIR") ?: "/tmp/agent-task-queue",
        var displayMode: String = "default",
        var notificationsEnabled: Boolean = true,
    )

    private var state = State()

    override fun getState(): State = state

    override fun loadState(state: State) {
        this.state = state
    }

    var dataDir: String
        get() = state.dataDir
        set(value) {
            state.dataDir = value
        }

    var displayMode: String
        get() = state.displayMode
        set(value) {
            state.displayMode = value
        }

    var notificationsEnabled: Boolean
        get() = state.notificationsEnabled
        set(value) {
            state.notificationsEnabled = value
        }

    val dbPath: String
        get() = "$dataDir/queue.db"

    val outputDir: String
        get() = "$dataDir/output"

    companion object {
        fun getInstance(): TaskQueueSettings =
            ApplicationManager.getApplication().getService(TaskQueueSettings::class.java)
    }
}
