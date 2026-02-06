package com.block.agenttaskqueue.settings

import com.intellij.openapi.options.Configurable
import com.intellij.openapi.ui.TextFieldWithBrowseButton
import com.intellij.ui.components.JBLabel
import com.intellij.util.ui.FormBuilder
import javax.swing.JComponent
import javax.swing.JPanel

class TaskQueueConfigurable : Configurable {

    private var panel: JPanel? = null
    private var dataDirField: TextFieldWithBrowseButton? = null

    override fun getDisplayName(): String = "Agent Task Queue"

    override fun createComponent(): JComponent {
        dataDirField = TextFieldWithBrowseButton().apply {
            text = TaskQueueSettings.getInstance().dataDir
            addBrowseFolderListener("Select Data Directory", "Choose the agent-task-queue data directory", null,
                com.intellij.openapi.fileChooser.FileChooserDescriptorFactory.createSingleFolderDescriptor())
        }

        panel = FormBuilder.createFormBuilder()
            .addLabeledComponent(JBLabel("Data directory:"), dataDirField!!, 1, false)
            .addComponentFillVertically(JPanel(), 0)
            .panel

        return panel!!
    }

    override fun isModified(): Boolean {
        return dataDirField?.text != TaskQueueSettings.getInstance().dataDir
    }

    override fun apply() {
        TaskQueueSettings.getInstance().dataDir = dataDirField?.text ?: return
    }

    override fun reset() {
        dataDirField?.text = TaskQueueSettings.getInstance().dataDir
    }

    override fun disposeUIResources() {
        panel = null
        dataDirField = null
    }
}
