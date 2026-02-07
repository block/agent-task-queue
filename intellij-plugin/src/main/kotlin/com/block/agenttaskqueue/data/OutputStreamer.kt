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
) {

    companion object {
        private val LOG = Logger.getInstance(OutputStreamer::class.java)
        private const val ACTIVE_POLL_MS = 50L
        private const val IDLE_POLL_MS = 200L
        private const val STDOUT_MARKER = "--- STDOUT ---\n"
    }

    private var currentTaskId: Int? = null
    private var currentLogPath: String? = null
    private var fileOffset: Long = 0
    private var tailJob: Job? = null
    private var headerSkipped = false

    fun startTailing(taskId: Int, logFilePath: String) {
        if (taskId == currentTaskId) return
        stopTailing()
        currentTaskId = taskId
        currentLogPath = logFilePath
        fileOffset = 0
        headerSkipped = false
        onClear()

        tailJob = scope.launch(Dispatchers.IO) {
            val file = File(logFilePath)
            while (isActive) {
                var hadNewData = false
                try {
                    if (file.exists()) {
                        if (!headerSkipped) {
                            // Skip past the header to the STDOUT marker
                            val content = file.readText(Charsets.UTF_8)
                            val markerIdx = content.indexOf(STDOUT_MARKER)
                            if (markerIdx >= 0) {
                                fileOffset = (markerIdx + STDOUT_MARKER.length).toLong()
                                headerSkipped = true
                            }
                        } else if (file.length() > fileOffset) {
                            RandomAccessFile(file, "r").use { raf ->
                                raf.seek(fileOffset)
                                val bytes = ByteArray((raf.length() - fileOffset).toInt())
                                raf.readFully(bytes)
                                fileOffset = raf.length()
                                val text = filterContent(String(bytes, Charsets.UTF_8))
                                if (text.isNotEmpty()) {
                                    onContent(text)
                                    hadNewData = true
                                }
                            }
                        }
                    }
                } catch (e: Exception) {
                    LOG.debug("Error tailing log file: $logFilePath", e)
                }
                delay(if (hadNewData) ACTIVE_POLL_MS else IDLE_POLL_MS)
            }
        }
    }

    fun finishTailing() {
        tailJob?.cancel()
        tailJob = null
        // Flush any remaining bytes written after the task finished
        val path = currentLogPath
        if (path != null && headerSkipped) {
            try {
                val file = File(path)
                if (file.exists() && file.length() > fileOffset) {
                    RandomAccessFile(file, "r").use { raf ->
                        raf.seek(fileOffset)
                        val bytes = ByteArray((raf.length() - fileOffset).toInt())
                        raf.readFully(bytes)
                        fileOffset = raf.length()
                        val text = filterContent(String(bytes, Charsets.UTF_8))
                        if (text.isNotEmpty()) {
                            onContent(text)
                        }
                    }
                }
            } catch (e: Exception) {
                LOG.debug("Error flushing log file: $path", e)
            }
        }
        currentTaskId = null
        currentLogPath = null
        fileOffset = 0
        headerSkipped = false
    }

    fun stopTailing() {
        tailJob?.cancel()
        tailJob = null
        currentTaskId = null
        currentLogPath = null
        fileOffset = 0
        headerSkipped = false
    }

    private fun filterContent(text: String): String {
        var result = text
        // Strip everything from SUMMARY marker onwards
        val summaryIdx = result.indexOf("--- SUMMARY ---")
        if (summaryIdx >= 0) {
            result = result.substring(0, summaryIdx).trimEnd('\n')
        }
        // Strip STDERR marker line but keep stderr content
        result = result.replace("--- STDERR ---\n", "")
        return result
    }
}
