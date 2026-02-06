package com.block.agenttaskqueue.model

data class QueueTask(
    val id: Int,
    val queueName: String,
    val status: String,
    val command: String?,
    val pid: Int?,
    val childPid: Int?,
    val createdAt: String?,
    val updatedAt: String?,
)
