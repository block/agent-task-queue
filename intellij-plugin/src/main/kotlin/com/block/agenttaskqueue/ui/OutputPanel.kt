package com.block.agenttaskqueue.ui

import com.block.agenttaskqueue.data.OutputStreamer
import com.block.agenttaskqueue.model.QueueSummary
import com.block.agenttaskqueue.model.QueueTask
import com.block.agenttaskqueue.model.TaskQueueListener
import com.block.agenttaskqueue.model.TaskQueueModel
import com.intellij.execution.filters.TextConsoleBuilderFactory
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

class OutputPanel(
    private val project: Project,
    val taskId: Int,
    logFilePath: String,
) : JPanel(BorderLayout()), Disposable {

    private val consoleView =
        TextConsoleBuilderFactory.getInstance().createBuilder(project).console
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var finished = false

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
    )

    init {
        add(consoleView.component, BorderLayout.CENTER)
        streamer.startTailing(taskId, logFilePath)

        // Watch for this task to disappear (finished) so we flush remaining output
        project.messageBus.connect(this).subscribe(TaskQueueModel.TOPIC, object : TaskQueueListener {
            override fun onQueueUpdated(tasks: List<QueueTask>, summary: QueueSummary) {
                if (!finished && tasks.none { it.id == taskId }) {
                    finished = true
                    streamer.finishTailing()
                }
            }
        })
    }

    override fun dispose() {
        streamer.stopTailing()
        scope.coroutineContext[kotlinx.coroutines.Job.Key]?.cancel()
        Disposer.dispose(consoleView)
    }
}
