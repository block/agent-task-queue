package com.block.agenttaskqueue.sidecar

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.nio.file.Path

data class HistoricalTaskUsage(
    val pid: Int,
    val timestamp: String,
    val workingDirectory: String? = null,
    val worktreeRoot: String? = null,
    val repoName: String? = null,
    val gitBranch: String? = null,
    val agentName: String? = null,
) {
    val displayAgentLabel: String?
        get() = agentName
            ?.trim()
            ?.takeIf { it.isNotBlank() }
            ?.let(::normalizeAgentLabel)

    val displayContextLabel: String?
        get() = buildList {
            repoName?.trim()?.takeIf { it.isNotBlank() }?.let(::add)
            gitBranch?.trim()?.takeIf { it.isNotBlank() }?.let(::add)

            if (gitBranch.isNullOrBlank()) {
                worktreeRoot?.trim()?.takeIf { it.isNotBlank() }?.let(::compactPathLabel)?.let(::add)
            }
            if (isEmpty()) {
                workingDirectory?.trim()?.takeIf { it.isNotBlank() }?.let(::compactPathLabel)?.let(::add)
            }
        }
            .distinct()
            .joinToString(" · ")
            .takeIf { it.isNotBlank() }
}

data class QueueMetricsSnapshot(
    val latestUsageByPid: Map<Int, HistoricalTaskUsage>,
) {
    companion object {
        val EMPTY = QueueMetricsSnapshot(latestUsageByPid = emptyMap())
    }
}

object TaskQueueMetrics {
    fun loadSnapshot(dataDir: Path): QueueMetricsSnapshot {
        val metricsPath = dataDir.resolve("agent-task-queue-logs.json")
        if (!metricsPath.toFile().exists()) {
            return QueueMetricsSnapshot.EMPTY
        }

        return runCatching {
            parseMetricsSnapshot(metricsPath.toFile().readText())
        }.getOrElse {
            QueueMetricsSnapshot.EMPTY
        }
    }
}

internal fun parseMetricsSnapshot(output: String): QueueMetricsSnapshot {
    val latestUsageByPid = linkedMapOf<Int, HistoricalTaskUsage>()

    output.lineSequence()
        .map(String::trim)
        .filter(String::isNotBlank)
        .forEach { line ->
            val entry = runCatching { Json.parseToJsonElement(line).jsonObject }.getOrNull() ?: return@forEach
            val pid = entry["pid"]?.jsonPrimitive?.content?.toIntOrNull() ?: return@forEach
            val timestamp = entry["timestamp"]?.jsonPrimitive?.contentOrNull ?: return@forEach

            val usage = HistoricalTaskUsage(
                pid = pid,
                timestamp = timestamp,
                workingDirectory = entry["working_directory"]?.jsonPrimitive?.contentOrNull,
                worktreeRoot = entry["worktree_root"]?.jsonPrimitive?.contentOrNull,
                repoName = entry["repo_name"]?.jsonPrimitive?.contentOrNull,
                gitBranch = entry["git_branch"]?.jsonPrimitive?.contentOrNull,
                agentName = entry["agent_name"]?.jsonPrimitive?.contentOrNull,
            )

            if (usage.displayAgentLabel == null && usage.displayContextLabel == null) {
                return@forEach
            }

            val existing = latestUsageByPid[pid]
            if (existing == null || usage.timestamp > existing.timestamp) {
                latestUsageByPid[pid] = usage
            }
        }

    return QueueMetricsSnapshot(latestUsageByPid = latestUsageByPid)
}
