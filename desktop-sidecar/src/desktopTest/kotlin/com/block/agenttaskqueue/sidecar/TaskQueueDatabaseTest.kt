package com.block.agenttaskqueue.sidecar

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class TaskQueueDatabaseTest {
    @Test
    fun coerceNullableIntKeepsInRangeValues() {
        assertEquals(42, coerceNullableInt(42L))
        assertEquals(7, coerceNullableInt("7"))
    }

    @Test
    fun coerceNullableIntRejectsOutOfRangeValues() {
        assertNull(coerceNullableInt(Long.MAX_VALUE))
        assertNull(coerceNullableInt("2147483648"))
    }
}
