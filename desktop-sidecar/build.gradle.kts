plugins {
    kotlin("multiplatform") version "2.3.20"
    kotlin("plugin.compose") version "2.3.20"
    id("org.jetbrains.compose") version "1.10.3"
}

repositories {
    mavenCentral()
    google()
}

kotlin {
    jvm("desktop")
    jvmToolchain(21)

    sourceSets {
        val desktopMain by getting {
            dependencies {
                implementation(compose.desktop.currentOs)
                implementation(compose.foundation)
                implementation(compose.material3)
                implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.10.2")
                implementation("org.xerial:sqlite-jdbc:3.53.0.0")
            }
        }
    }
}

compose.desktop {
    application {
        mainClass = "com.block.agenttaskqueue.sidecar.MainKt"

        nativeDistributions {
            targetFormats(org.jetbrains.compose.desktop.application.dsl.TargetFormat.Dmg)
            packageName = "AgentTaskQueueSidecar"
            packageVersion = "1.0.0"
            description = "Desktop sidecar for visualizing agent-task-queue state"
        }
    }
}
