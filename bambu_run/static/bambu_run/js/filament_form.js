/**
 * filament_form.js — Filament add/edit form interactions.
 *
 * Handles:
 *   - Filament type preset → auto-fill Type / Sub Type / Brand
 *   - Transparent checkbox → toggle color picker vs. checkerboard swatch
 *   - Color picker ↔ hex text sync
 *   - Initial / remaining weight ↔ remaining percent sync
 *   - Delete confirmation modal
 */

document.addEventListener('DOMContentLoaded', function () {

    // ── Filament type preset auto-fill ────────────────────────────────────────

    const dataEl = document.getElementById('filament-type-data');
    const filamentTypeMap = dataEl ? JSON.parse(dataEl.textContent) : {};

    const filamentTypeSelect = document.getElementById('id_filament_type');
    const typeField    = document.getElementById('id_type');
    const subTypeField = document.getElementById('id_sub_type');
    const brandField   = document.getElementById('id_brand');

    if (filamentTypeSelect) {
        filamentTypeSelect.addEventListener('change', function () {
            const mapping = filamentTypeMap[this.value];
            if (mapping && typeField && subTypeField && brandField) {
                typeField.value    = mapping.type;
                subTypeField.value = mapping.sub_type;
                brandField.value   = mapping.brand;
            }
        });
    }

    // ── AMS unit → tray choices ───────────────────────────────────────────────

    const amsDataEl = document.getElementById('ams-slot-data');
    const amsUnits = amsDataEl ? JSON.parse(amsDataEl.textContent) : [];
    const printerSelect = document.getElementById('id_current_printer');
    const amsUnitSelect = document.getElementById('id_ams_unit_id');
    const traySelect = document.getElementById('id_current_tray_id');

    function option(value, label) {
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = label;
        return opt;
    }

    function syncAmsUnitChoices() {
        if (!printerSelect || !amsUnitSelect || !amsUnits.length) return;

        const currentUnit = amsUnitSelect.value;
        const selectedPrinterId = printerSelect.value;
        const matchingUnits = amsUnits.filter(function (unit) {
            return !selectedPrinterId || String(unit.printer_id) === String(selectedPrinterId);
        });

        amsUnitSelect.replaceChildren(option('', '--- Select AMS Unit ---'));
        matchingUnits.forEach(function (unit) {
            amsUnitSelect.appendChild(option(unit.unit_id, unit.label));
        });

        if (matchingUnits.some(function (unit) { return String(unit.unit_id) === String(currentUnit); })) {
            amsUnitSelect.value = currentUnit;
        }
    }

    function syncTrayChoices() {
        if (!amsUnitSelect || !traySelect || !amsUnits.length) return;

        const selectedPrinterId = printerSelect ? printerSelect.value : '';
        const currentTray = traySelect.value;
        const selectedUnit = amsUnits.find(function (unit) {
            const printerMatches = !selectedPrinterId || String(unit.printer_id) === String(selectedPrinterId);
            return printerMatches && String(unit.unit_id) === String(amsUnitSelect.value);
        });
        const trayIds = selectedUnit ? selectedUnit.tray_ids : [0, 1, 2, 3];

        traySelect.replaceChildren(option('', '--- Select Tray ---'));
        trayIds.forEach(function (trayId) {
            traySelect.appendChild(option(trayId, 'Tray ' + trayId));
        });

        if (trayIds.map(String).includes(String(currentTray))) {
            traySelect.value = currentTray;
        }
    }

    if (amsUnitSelect && traySelect) {
        syncAmsUnitChoices();
        syncTrayChoices();
        if (printerSelect) {
            printerSelect.addEventListener('change', function () {
                syncAmsUnitChoices();
                syncTrayChoices();
            });
        }
        amsUnitSelect.addEventListener('change', syncTrayChoices);
    }

    // ── Transparent toggle ────────────────────────────────────────────────────

    const transparentCheckbox = document.getElementById('id_is_transparent');
    const transparentSwatch   = document.getElementById('transparent-swatch');
    const colorSelect         = document.getElementById('id_color');
    const colorPicker         = document.getElementById('id_color_hex_picker');
    const colorText           = document.getElementById('id_color_hex_text');

    /**
     * Show checkerboard swatch and disable color inputs when transparent,
     * restore normal color picker when not transparent.
     * @param {boolean} isTransparent
     */
    function applyTransparentState(isTransparent) {
        if (!colorPicker) return;
        if (isTransparent) {
            transparentSwatch.style.display = 'block';
            colorPicker.style.display       = 'none';
            colorPicker.disabled            = true;
            if (colorText) { colorText.disabled = true; colorText.value = ''; }
        } else {
            transparentSwatch.style.display = 'none';
            colorPicker.style.display       = '';
            colorPicker.disabled            = false;
            if (colorText) { colorText.disabled = false; }
        }
    }

    function setColorInputs(hexValue) {
        if (!/^#[0-9A-Fa-f]{6}$/.test(hexValue)) return;

        const normalized = hexValue.toUpperCase();
        if (colorPicker) {
            colorPicker.value = normalized;
        }
        if (colorText) {
            colorText.value = normalized;
            colorText.classList.remove('is-invalid');
        }
    }

    if (transparentCheckbox) {
        applyTransparentState(transparentCheckbox.checked);
        transparentCheckbox.addEventListener('change', function () {
            applyTransparentState(this.checked);
        });
    }

    if (colorSelect) {
        colorSelect.addEventListener('change', function () {
            const selected = this.options[this.selectedIndex];
            const managedHex = selected ? selected.dataset.colorHex : '';
            if (managedHex) {
                setColorInputs(managedHex);
            }
        });
    }

    // ── Color picker ↔ hex text sync ──────────────────────────────────────────

    if (colorPicker && colorText) {
        colorPicker.addEventListener('input', function () {
            setColorInputs(this.value);
        });

        colorText.addEventListener('input', function () {
            const value = this.value.trim();
            if (/^#[0-9A-Fa-f]{6}$/.test(value)) {
                setColorInputs(value);
                this.classList.remove('is-invalid');
            } else if (value.length === 7) {
                this.classList.add('is-invalid');
            }
        });

        if (colorText.value && /^#[0-9A-Fa-f]{6}$/.test(colorText.value)) {
            setColorInputs(colorText.value);
        } else if (colorPicker.value && !colorText.value) {
            setColorInputs(colorPicker.value);
        }
    }

    // ── Initial / remaining weight sync ──────────────────────────────────────

    const initialWeightField = document.getElementById('id_initial_weight_grams');
    const remainingPercentField = document.getElementById('id_remaining_percent');
    const remainingWeightField = document.getElementById('id_remaining_weight_grams');
    const remainingSourceField = document.getElementById('id_remaining_source');

    function numberValue(field) {
        if (!field || field.value === '') return null;
        const value = Number(field.value);
        return Number.isFinite(value) ? value : null;
    }

    function clamp(value, min, max) {
        return Math.min(max, Math.max(min, value));
    }

    function roundTwo(value) {
        return Math.round((value + Number.EPSILON) * 100) / 100;
    }

    function formatDecimal(value) {
        return roundTwo(value).toFixed(2).replace(/\.?0+$/, '');
    }

    function setRemainingSource(source) {
        if (remainingSourceField) {
            remainingSourceField.value = source;
        }
    }

    function syncWeightFromPercent(options) {
        const normalizeSource = options && options.normalizeSource;
        const initialWeight = numberValue(initialWeightField);
        const remainingPercent = numberValue(remainingPercentField);
        if (!initialWeight || remainingPercent === null || !remainingWeightField) return;

        const boundedPercent = clamp(remainingPercent, 0, 100);
        if (normalizeSource) {
            remainingPercentField.value = formatDecimal(boundedPercent);
        }
        remainingWeightField.value = formatDecimal(initialWeight * (boundedPercent / 100));
    }

    function syncPercentFromWeight(options) {
        const normalizeSource = options && options.normalizeSource;
        const initialWeight = numberValue(initialWeightField);
        const remainingWeight = numberValue(remainingWeightField);
        if (!initialWeight || remainingWeight === null || !remainingPercentField) return;

        if (normalizeSource) {
            remainingWeightField.value = formatDecimal(Math.max(0, remainingWeight));
        }
        remainingPercentField.value = formatDecimal(clamp((remainingWeight / initialWeight) * 100, 0, 100));
    }

    if (initialWeightField && remainingPercentField && remainingWeightField) {
        if (!initialWeightField.value) {
            initialWeightField.value = '1000';
        }
        if (!remainingPercentField.value) {
            remainingPercentField.value = '100';
        }
        if (!remainingWeightField.value) {
            syncWeightFromPercent();
        }

        initialWeightField.addEventListener('input', function () {
            const source = remainingSourceField ? remainingSourceField.value : '';
            setRemainingSource('initial');
            if (source === 'weight' && remainingWeightField.value) {
                syncPercentFromWeight({ normalizeSource: true });
            } else {
                syncWeightFromPercent({ normalizeSource: true });
            }
        });

        remainingPercentField.addEventListener('input', function () {
            setRemainingSource('percent');
            syncWeightFromPercent();
        });

        remainingWeightField.addEventListener('input', function () {
            setRemainingSource('weight');
            syncPercentFromWeight();
        });

        remainingPercentField.addEventListener('blur', function () {
            setRemainingSource('percent');
            syncWeightFromPercent({ normalizeSource: true });
        });

        remainingWeightField.addEventListener('blur', function () {
            setRemainingSource('weight');
            syncPercentFromWeight({ normalizeSource: true });
        });
    }

    // ── Delete confirmation modal ─────────────────────────────────────────────

    const deleteConfirmText = document.getElementById('deleteConfirmText');
    const confirmDeleteBtn  = document.getElementById('confirmDeleteBtn');
    const deleteForm        = document.getElementById('deleteForm');
    const deleteModal       = document.getElementById('deleteModal');

    if (deleteConfirmText && confirmDeleteBtn) {
        deleteConfirmText.addEventListener('input', function () {
            const value = this.value.trim();
            if (value === 'DELETE') {
                confirmDeleteBtn.disabled = false;
                this.classList.remove('is-invalid');
                this.classList.add('is-valid');
            } else {
                confirmDeleteBtn.disabled = true;
                this.classList.remove('is-valid');
                if (value.length > 0) {
                    this.classList.add('is-invalid');
                } else {
                    this.classList.remove('is-invalid');
                }
            }
        });

        if (deleteForm) {
            deleteForm.addEventListener('submit', function (e) {
                if (confirmDeleteBtn.disabled) {
                    e.preventDefault();
                    alert('Please type DELETE to confirm deletion');
                    return false;
                }
                return true;
            });
        }

        if (deleteModal) {
            deleteModal.addEventListener('hidden.bs.modal', function () {
                deleteConfirmText.value = '';
                confirmDeleteBtn.disabled = true;
                deleteConfirmText.classList.remove('is-valid', 'is-invalid');
            });

            deleteModal.addEventListener('shown.bs.modal', function () {
                deleteConfirmText.focus();
            });
        }
    }

    // ── Delete button modal opener (backup) ───────────────────────────────────

    const deleteBtn = document.getElementById('deleteBtn');
    if (deleteBtn && deleteModal) {
        deleteBtn.addEventListener('click', function () {
            if (!deleteModal.classList.contains('show')) {
                if (typeof bootstrap !== 'undefined') {
                    bootstrap.Modal.getOrCreateInstance(deleteModal).show();
                } else if (typeof coreui !== 'undefined' && coreui.Modal) {
                    coreui.Modal.getOrCreateInstance(deleteModal).show();
                }
            }
        });
    }

});
