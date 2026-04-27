package com.block.agenttaskqueue.sidecar

import java.nio.file.Paths
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class EnvironmentSnapshotTest {
    @Test
    fun parsesLiveTaskQueueServerCapacitiesFromPsOutput() {
        val processes = parseTaskQueueProcesses(
            """
            73781 /Users/me/.venv/bin/python3 task_queue.py --data-dir /tmp/agent-task-queue --queue-capacity=gradle=2 --queue-capacity=gradle/emulator-5554=1
            73782 uv run --directory /repo python task_queue.py --queue-capacity=gradle=2
            73783 /Users/me/.venv/bin/python3 task_queue.py --data-dir=/tmp/other-queue --queue-capacity=web=3
            """.trimIndent()
        )

        assertEquals(2, processes.size)
        assertEquals(Paths.get("/tmp/agent-task-queue"), processes.first().dataDir)
        assertEquals(2, processes.first().queueCapacities["gradle"])
        assertEquals(1, processes.first().queueCapacities["gradle/emulator-5554"])
        assertEquals(Paths.get("/tmp/other-queue"), processes.last().dataDir)
    }

    @Test
    fun fallsBackToTaskQueueDataDirEnvironmentWhenFlagIsOmitted() {
        val process = parseTaskQueueProcesses(
            """
            73781 1 /Users/me/.venv/bin/python3 task_queue.py --queue-capacity=gradle=2 TASK_QUEUE_DATA_DIR=/tmp/custom-queue
            """.trimIndent()
        ).single()

        assertEquals(Paths.get("/tmp/custom-queue"), process.dataDir)
        assertEquals(2, process.queueCapacities["gradle"])
    }

    @Test
    fun parsesAdbDevicesAndMatchesEmulatorPorts() {
        val adb = parseAdbSnapshot(
            """
            List of devices attached
            emulator-5554 device product:sdk_gphone64_arm64 model:sdk_gphone64_arm64 device:emu64a transport_id:1
            127.0.0.1:5557 device transport_id:2
            R58N12345AB unauthorized transport_id:3
            """.trimIndent()
        )

        assertEquals(3, adb.devices.size)
        assertEquals(2, adb.connectedDevices)
        assertEquals(2, adb.connectedEmulators)
        val devicesBySerial = adb.devices.associateBy { it.serial }
        assertEquals("5554", devicesBySerial.getValue("emulator-5554").emulatorPort)
        assertTrue(devicesBySerial.getValue("127.0.0.1:5557").isEmulator)
        assertNull(devicesBySerial.getValue("R58N12345AB").emulatorPort)
    }

    @Test
    fun extractsEmulatorPortFromQueueAndAdbLabels() {
        assertEquals("5554", extractEmulatorPort("emulator-5554"))
        assertEquals("5557", extractEmulatorPort("emu-5557"))
        assertEquals("5559", extractEmulatorPort("127.0.0.1:5559"))
        assertNull(extractEmulatorPort("pixel-9-pro"))
    }

    @Test
    fun infersAgentAndContextFromServerParentProcesses() {
        val process = parseTaskQueueProcesses(
            """
            900 1 amp -m deep
            901 900 uv run --directory /Users/me/Development/agent-task-queue-worktrees/queue-visibility python task_queue.py --data-dir /tmp/agent-task-queue --queue-capacity=gradle=2
            902 901 /Users/me/.venv/bin/python3 task_queue.py --data-dir /tmp/agent-task-queue --queue-capacity=gradle=2
            """.trimIndent()
        ).single()

        assertEquals(902, process.pid)
        assertEquals(901, process.parentPid)
        assertEquals("Amp deep", process.agentLabel)
        assertEquals("queue-visibility", process.contextLabel)
    }
}
