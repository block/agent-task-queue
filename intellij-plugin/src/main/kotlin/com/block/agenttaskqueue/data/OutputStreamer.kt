package com.block.agenttaskqueue.data

import com.intellij.openapi.diagnostic.Logger
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.io.File
import java.io.RandomAccessFile

class OutputStreamer(
    private val scope: CoroutineScope,
    private val onContent: (String) -> Unit,
    private val onClear: () -> Unit,
    private val onNoTask: () -> Unit,
) {

    companion object {
        private val LOG = Logger.getInstance(OutputStreamer::class.java)
        private const val ACTIVE_POLL_MS = 50L
        private const val IDLE_POLL_MS = 200L
    }

    private var currentTaskId: Int? = null
    private var fileOffset: Long = 0
    private var tailJob: Job? = null

    fun startTailing(taskId: Int, logFilePath: String) {
        if (taskId == currentTaskId) return
        stopTailing()
        currentTaskId = taskId
        fileOffset = 0
        onClear()

        tailJob = scope.launch(Dispatchers.IO) {
            val file = File(logFilePath)
            while (isActive) {
                var hadNewData = false
                try {
                    if (file.exists() && file.length() > fileOffset) {
                        RandomAccessFile(file, "r").use { raf ->
                            raf.seek(fileOffset)
                            val bytes = ByteArray((raf.length() - fileOffset).toInt())
                            raf.readFully(bytes)
                            fileOffset = raf.length()
                            val text = String(bytes, Charsets.UTF_8)
                            onContent(text)
                            hadNewData = true
                        }
                    }
                } catch (e: Exception) {
                    LOG.debug("Error tailing log file: $logFilePath", e)
                }
                // Poll faster when output is actively flowing, slower when idle
                delay(if (hadNewData) ACTIVE_POLL_MS else IDLE_POLL_MS)
            }
        }
    }

    fun stopTailing() {
        tailJob?.cancel()
        tailJob = null
        currentTaskId = null
        fileOffset = 0
    }

    fun showNoTask() {
        stopTailing()
        onNoTask()
    }
}
