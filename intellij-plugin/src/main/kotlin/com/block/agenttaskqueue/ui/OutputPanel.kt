package com.block.agenttaskqueue.ui

import com.block.agenttaskqueue.data.OutputStreamer
import com.block.agenttaskqueue.model.QueueSummary
import com.block.agenttaskqueue.model.QueueTask
import com.block.agenttaskqueue.model.TaskQueueListener
import com.block.agenttaskqueue.model.TaskQueueModel
import com.block.agenttaskqueue.settings.TaskQueueSettings
import com.intellij.execution.filters.TextConsoleBuilderFactory
import com.intellij.execution.ui.ConsoleView
import com.intellij.execution.ui.ConsoleViewContentType
import com.intellij.openapi.Disposable
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.Disposer
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import java.awt.BorderLayout
import javax.swing.JPanel

class OutputPanel(project: Project) : JPanel(BorderLayout()), Disposable {

    private val consoleView: ConsoleView =
        TextConsoleBuilderFactory.getInstance().createBuilder(project).console
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var currentRunningTaskId: Int? = null

    private val streamer = OutputStreamer(
        scope = scope,
        onContent = { text ->
            ApplicationManager.getApplication().invokeLater {
                consoleView.print(text, ConsoleViewContentType.NORMAL_OUTPUT)
            }
        },
        onClear = {
            ApplicationManager.getApplication().invokeLater {
                consoleView.clear()
            }
        },
        onNoTask = {
            ApplicationManager.getApplication().invokeLater {
                consoleView.clear()
                consoleView.print("No task running\n", ConsoleViewContentType.SYSTEM_OUTPUT)
            }
        }
    )

    init {
        Disposer.register(project, this)
        add(consoleView.component, BorderLayout.CENTER)

        project.messageBus.connect(this).subscribe(TaskQueueModel.TOPIC, object : TaskQueueListener {
            override fun onQueueUpdated(tasks: List<QueueTask>, summary: QueueSummary) {
                val runningTask = tasks.firstOrNull { it.status == "running" }
                handleRunningTask(runningTask)
            }
        })

        // Initial state
        val runningTask = TaskQueueModel.getInstance().tasks.firstOrNull { it.status == "running" }
        handleRunningTask(runningTask)
    }

    private fun handleRunningTask(task: QueueTask?) {
        if (task == null) {
            currentRunningTaskId = null
            streamer.showNoTask()
            return
        }

        if (task.id != currentRunningTaskId) {
            currentRunningTaskId = task.id
            val outputDir = TaskQueueSettings.getInstance().outputDir
            val logPath = "$outputDir/task_${task.id}.log"
            streamer.startTailing(task.id, logPath)
        }
    }

    override fun dispose() {
        streamer.stopTailing()
        scope.coroutineContext[kotlinx.coroutines.Job.Key]?.cancel()
        Disposer.dispose(consoleView)
    }
}
