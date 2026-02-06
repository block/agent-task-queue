package com.block.agenttaskqueue.settings

import com.intellij.openapi.options.Configurable
import com.intellij.openapi.ui.ComboBox
import com.intellij.openapi.ui.TextFieldWithBrowseButton
import com.intellij.ui.components.JBCheckBox
import com.intellij.ui.components.JBLabel
import com.intellij.util.ui.FormBuilder
import javax.swing.DefaultComboBoxModel
import javax.swing.JComponent
import javax.swing.JPanel

class TaskQueueConfigurable : Configurable {

    private var panel: JPanel? = null
    private var dataDirField: TextFieldWithBrowseButton? = null
    private var displayModeCombo: ComboBox<String>? = null
    private var notificationsCheckbox: JBCheckBox? = null

    private val displayModes = arrayOf("hidden", "minimal", "default", "verbose")
    private val displayModeLabels = arrayOf("Hidden", "Minimal (icon only)", "Default", "Verbose (with elapsed time)")

    override fun getDisplayName(): String = "Agent Task Queue"

    override fun createComponent(): JComponent {
        val settings = TaskQueueSettings.getInstance()

        dataDirField = TextFieldWithBrowseButton().apply {
            text = settings.dataDir
            addBrowseFolderListener("Select Data Directory", "Choose the agent-task-queue data directory", null,
                com.intellij.openapi.fileChooser.FileChooserDescriptorFactory.createSingleFolderDescriptor())
        }

        displayModeCombo = ComboBox(DefaultComboBoxModel(displayModeLabels)).apply {
            selectedIndex = displayModes.indexOf(settings.displayMode).coerceAtLeast(0)
        }

        notificationsCheckbox = JBCheckBox("Enable notifications", settings.notificationsEnabled)

        panel = FormBuilder.createFormBuilder()
            .addLabeledComponent(JBLabel("Data directory:"), dataDirField!!, 1, false)
            .addLabeledComponent(JBLabel("Status bar display:"), displayModeCombo!!, 1, false)
            .addComponent(notificationsCheckbox!!, 1)
            .addComponentFillVertically(JPanel(), 0)
            .panel

        return panel!!
    }

    override fun isModified(): Boolean {
        val settings = TaskQueueSettings.getInstance()
        return dataDirField?.text != settings.dataDir
                || displayModes.getOrNull(displayModeCombo?.selectedIndex ?: -1) != settings.displayMode
                || notificationsCheckbox?.isSelected != settings.notificationsEnabled
    }

    override fun apply() {
        val settings = TaskQueueSettings.getInstance()
        settings.dataDir = dataDirField?.text ?: return
        settings.displayMode = displayModes.getOrNull(displayModeCombo?.selectedIndex ?: -1) ?: "default"
        settings.notificationsEnabled = notificationsCheckbox?.isSelected ?: true
    }

    override fun reset() {
        val settings = TaskQueueSettings.getInstance()
        dataDirField?.text = settings.dataDir
        displayModeCombo?.selectedIndex = displayModes.indexOf(settings.displayMode).coerceAtLeast(0)
        notificationsCheckbox?.isSelected = settings.notificationsEnabled
    }

    override fun disposeUIResources() {
        panel = null
        dataDirField = null
        displayModeCombo = null
        notificationsCheckbox = null
    }
}
