package com.block.agenttaskqueue.ui

import com.block.agenttaskqueue.model.QueueTask
import javax.swing.table.AbstractTableModel

class TaskQueueTableModel : AbstractTableModel() {

    private var tasks: List<QueueTask> = emptyList()

    private val columns = arrayOf("#", "Status", "Queue", "Command", "Time")

    override fun getColumnCount(): Int = columns.size

    override fun getRowCount(): Int = tasks.size

    override fun getColumnName(column: Int): String = columns[column]

    override fun getValueAt(rowIndex: Int, columnIndex: Int): Any? {
        val task = tasks.getOrNull(rowIndex) ?: return null
        return when (columnIndex) {
            0 -> task.id
            1 -> task.status
            2 -> task.queueName
            3 -> task.command ?: ""
            4 -> relativeTime(task.createdAt)
            else -> null
        }
    }

    fun getTaskAt(row: Int): QueueTask? = tasks.getOrNull(row)

    fun updateTasks(newTasks: List<QueueTask>) {
        tasks = newTasks
        fireTableDataChanged()
    }

    private fun relativeTime(timestamp: String?): String {
        if (timestamp == null) return ""
        try {
            val created = java.time.LocalDateTime.parse(timestamp.replace(" ", "T"))
                .atZone(java.time.ZoneOffset.UTC)
                .toInstant()
            val seconds = java.time.Duration.between(created, java.time.Instant.now()).seconds
            return when {
                seconds < 60 -> "${seconds}s ago"
                seconds < 3600 -> "${seconds / 60}m ago"
                else -> "${seconds / 3600}h ago"
            }
        } catch (e: Exception) {
            return timestamp
        }
    }
}
