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

/**
 * Tails task output files and streams content to the output panel.
 *
 * Supports two output formats:
 *   - task_<id>.raw.log — raw stdout+stderr with no metadata. Added in MCP server v0.4.0.
 *     Preferred; tailed directly from offset 0 with no filtering.
 *   - task_<id>.log — formatted log with COMMAND/STDOUT/STDERR/SUMMARY markers.
 *     Written by all MCP server versions. Used as fallback for MCP server v0.3.x and earlier
 *     which don't write .raw.log files. Requires skipping the header and filtering out
 *     section markers.
 */
class OutputStreamer(
    private val scope: CoroutineScope,
    private val onContent: (String) -> Unit,
    private val onClear: () -> Unit,
) {

    companion object {
        private val LOG = Logger.getInstance(OutputStreamer::class.java)
        private const val ACTIVE_POLL_MS = 50L
        private const val IDLE_POLL_MS = 200L
        // Fallback constants for MCP server v0.3.x and earlier that only write .log files
        private const val STDOUT_MARKER = "--- STDOUT ---\n"
        private const val MAX_FALLBACK_WAIT_MS = 2000L
    }

    private var currentTaskId: Int? = null
    private var currentLogPath: String? = null
    private var fileOffset: Long = 0
    private var tailJob: Job? = null
    private var useFallback = false

    fun startTailing(taskId: Int, logFilePath: String) {
        if (taskId == currentTaskId) return
        stopTailing()
        currentTaskId = taskId
        currentLogPath = logFilePath
        fileOffset = 0
        useFallback = false
        onClear()

        tailJob = scope.launch(Dispatchers.IO) {
            // Prefer .raw.log, fall back to .log (with filtering) for old servers
            val rawFile = File(logFilePath)
            val fallbackFile = File(logFilePath.removeSuffix(".raw.log") + ".log")
            var file: File? = null
            var waited = 0L

            // Wait briefly for the raw file to appear; fall back to formatted log
            while (isActive && file == null) {
                if (rawFile.exists()) {
                    file = rawFile
                } else if (waited >= MAX_FALLBACK_WAIT_MS && fallbackFile.exists()) {
                    file = fallbackFile
                    useFallback = true
                    LOG.info("Falling back to formatted log for task $taskId")
                } else {
                    delay(IDLE_POLL_MS)
                    waited += IDLE_POLL_MS
                }
            }

            if (file == null) return@launch

            // For fallback mode, skip past the header to the STDOUT marker
            if (useFallback) {
                while (isActive) {
                    val content = file.readText(Charsets.UTF_8)
                    val markerIdx = content.indexOf(STDOUT_MARKER)
                    if (markerIdx >= 0) {
                        fileOffset = (markerIdx + STDOUT_MARKER.length).toLong()
                        break
                    }
                    delay(IDLE_POLL_MS)
                }
            }

            while (isActive) {
                var hadNewData = false
                try {
                    if (file.length() > fileOffset) {
                        RandomAccessFile(file, "r").use { raf ->
                            raf.seek(fileOffset)
                            val bytes = ByteArray((raf.length() - fileOffset).toInt())
                            raf.readFully(bytes)
                            fileOffset = raf.length()
                            val text = if (useFallback) {
                                filterContent(String(bytes, Charsets.UTF_8))
                            } else {
                                String(bytes, Charsets.UTF_8)
                            }
                            if (text.isNotEmpty()) {
                                onContent(text)
                                hadNewData = true
                            }
                        }
                    }
                } catch (e: Exception) {
                    LOG.debug("Error tailing log file: ${file.path}", e)
                }
                delay(if (hadNewData) ACTIVE_POLL_MS else IDLE_POLL_MS)
            }
        }
    }

    fun finishTailing() {
        tailJob?.cancel()
        tailJob = null
        // Flush any remaining bytes written after the task finished
        val path = currentLogPath ?: return
        val file = if (useFallback) {
            File(path.removeSuffix(".raw.log") + ".log")
        } else {
            File(path)
        }
        try {
            if (file.exists() && file.length() > fileOffset) {
                RandomAccessFile(file, "r").use { raf ->
                    raf.seek(fileOffset)
                    val bytes = ByteArray((raf.length() - fileOffset).toInt())
                    raf.readFully(bytes)
                    fileOffset = raf.length()
                    val text = if (useFallback) {
                        filterContent(String(bytes, Charsets.UTF_8))
                    } else {
                        String(bytes, Charsets.UTF_8)
                    }
                    if (text.isNotEmpty()) {
                        onContent(text)
                    }
                }
            }
        } catch (e: Exception) {
            LOG.debug("Error flushing log file: ${file.path}", e)
        }
        currentTaskId = null
        currentLogPath = null
        fileOffset = 0
        useFallback = false
    }

    fun stopTailing() {
        tailJob?.cancel()
        tailJob = null
        currentTaskId = null
        currentLogPath = null
        fileOffset = 0
        useFallback = false
    }

    private fun filterContent(text: String): String {
        var result = text
        val summaryIdx = result.indexOf("--- SUMMARY ---")
        if (summaryIdx >= 0) {
            result = result.substring(0, summaryIdx).trimEnd('\n')
        }
        result = result.replace("--- STDERR ---\n", "")
        return result
    }
}
