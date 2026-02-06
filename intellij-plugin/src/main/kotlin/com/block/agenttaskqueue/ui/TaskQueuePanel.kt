package com.block.agenttaskqueue.ui

import com.block.agenttaskqueue.actions.TaskQueueDataKeys
import com.block.agenttaskqueue.data.TaskQueuePoller
import com.block.agenttaskqueue.model.QueueSummary
import com.block.agenttaskqueue.model.QueueTask
import com.block.agenttaskqueue.model.TaskQueueListener
import com.block.agenttaskqueue.model.TaskQueueModel
import com.intellij.openapi.actionSystem.ActionGroup
import com.intellij.openapi.actionSystem.ActionManager
import com.intellij.openapi.actionSystem.DataProvider
import com.intellij.openapi.project.Project
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.table.JBTable
import java.awt.BorderLayout
import javax.swing.JPanel

class TaskQueuePanel(private val project: Project) : JPanel(BorderLayout()), DataProvider {

    private val tableModel = TaskQueueTableModel()
    private val table = JBTable(tableModel)
    private val summaryLabel = JBLabel("Queue is empty")

    init {
        val group = ActionManager.getInstance().getAction("AgentTaskQueueToolbar") as ActionGroup
        val toolbar = ActionManager.getInstance().createActionToolbar("AgentTaskQueueToolbar", group, true)
        toolbar.targetComponent = this
        add(toolbar.component, BorderLayout.NORTH)

        // Column sizing: #, Status, Queue are fixed-width; Command gets the remaining space
        table.columnModel.getColumn(0).preferredWidth = 40   // #
        table.columnModel.getColumn(0).maxWidth = 60
        table.columnModel.getColumn(1).preferredWidth = 70   // Status
        table.columnModel.getColumn(1).maxWidth = 90
        table.columnModel.getColumn(2).preferredWidth = 80   // Queue
        table.columnModel.getColumn(2).maxWidth = 120
        table.columnModel.getColumn(3).preferredWidth = 400  // Command
        table.columnModel.getColumn(4).preferredWidth = 80   // Time
        table.columnModel.getColumn(4).maxWidth = 100
        table.autoResizeMode = javax.swing.JTable.AUTO_RESIZE_SUBSEQUENT_COLUMNS

        add(JBScrollPane(table), BorderLayout.CENTER)

        add(summaryLabel, BorderLayout.SOUTH)

        project.messageBus.connect().subscribe(TaskQueueModel.TOPIC, object : TaskQueueListener {
            override fun onQueueUpdated(tasks: List<QueueTask>, summary: QueueSummary) {
                tableModel.updateTasks(tasks)
                summaryLabel.text = if (tasks.isEmpty()) {
                    "Queue is empty"
                } else {
                    "${summary.running} running, ${summary.waiting} waiting (${summary.total} total)"
                }
            }
        })

        // Ensure polling is started
        TaskQueuePoller.getInstance()
    }

    override fun getData(dataId: String): Any? {
        if (dataId == TaskQueueDataKeys.SELECTED_TASK.name) {
            val selectedRow = table.selectedRow
            if (selectedRow >= 0) {
                return tableModel.getTaskAt(selectedRow)
            }
        }
        return null
    }
}
