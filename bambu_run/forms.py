from django import forms
from decimal import Decimal, ROUND_HALF_UP
from django.utils import timezone
from .models import Filament, FilamentColor, FilamentType, FILAMENT_COLOR_FINISH_CHOICES


class FilamentColorSelect(forms.Select):
    """Select widget that carries managed color metadata on each option."""

    option_attrs = None

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if self.option_attrs and subindex is None:
            try:
                option_attrs = self.option_attrs[index]
            except (IndexError, TypeError):
                option_attrs = {}
            option["attrs"].update(option_attrs)
        return option


class FilamentTypeForm(forms.ModelForm):
    """Form for managing FilamentType registry"""

    PRESET_TYPES = ['PLA', 'PETG', 'PET', 'ABS', 'ASA', 'TPU', 'PA', 'PC', 'PPS']
    PRESET_SUB_TYPES = [
        'PLA Basic', 'PLA Matte', 'PLA Silk', 'PLA Metal', 'PLA Marble', 'PLA Glow', 'PLA-CF',
        'PETG Basic', 'PETG-CF', 'PETG-HF', 'ABS', 'TPU 95A', 'PA6-CF', 'ASA', 'PC', 'PPS-CF',
        'Support W', 'Support G',
    ]
    PRESET_BRANDS = [
        'Bambu Lab', 'eSUN', 'Polymaker', 'Hatchbox', 'Prusament',
        'MatterHackers', 'Overture', '3DXTech', 'ColorFabb',
    ]

    class Meta:
        model = FilamentType
        fields = ['type', 'sub_type', 'brand']
        widgets = {
            'type': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g., PLA, PETG, ABS'
            }),
            'sub_type': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g., PLA Basic, PLA Matte (optional)'
            }),
            'brand': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g., Bambu Lab'
            }),
        }


class FilamentForm(forms.ModelForm):
    remaining_source = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={'id': 'id_remaining_source'})
    )
    location_target = forms.CharField(
        required=False,
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_location_target'})
    )
    location_tray_id = forms.CharField(
        required=False,
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_location_tray_id'})
    )

    color_hex_text = forms.CharField(
        required=False,
        max_length=7,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': '#000000',
            'pattern': '#[0-9A-Fa-f]{6}',
            'id': 'id_color_hex_text'
        }),
        label='Color Hex Code'
    )

    class Meta:
        model = Filament
        fields = [
            'tray_uuid', 'tag_uid', 'tag_id', 'created_by',
            'filament_type', 'type', 'sub_type', 'brand', 'color', 'color_hex', 'is_transparent',
            'diameter', 'initial_weight_grams',
            'remaining_percent', 'remaining_weight_grams',
            'is_loaded_in_ams', 'is_loaded_externally', 'current_printer', 'current_tray_id', 'ams_unit_id', 'ams_type',
            'purchase_date', 'purchase_price', 'supplier', 'notes'
        ]
        widgets = {
            'tray_uuid': forms.TextInput(attrs={
                'class': 'form-control font-monospace',
                'placeholder': 'Optional - Auto-filled by MQTT',
                'style': 'font-size: 0.9em;'
            }),
            'tag_uid': forms.TextInput(attrs={
                'class': 'form-control font-monospace',
                'placeholder': 'Optional - RFID chip ID',
                'style': 'font-size: 0.9em;'
            }),
            'tag_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Optional - User-defined ID'}),
            'created_by': forms.Select(attrs={'class': 'form-select'}),
            'filament_type': forms.Select(attrs={'class': 'form-select', 'id': 'id_filament_type'}),
            'type': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., PLA, PETG, ABS'}),
            'sub_type': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., PLA Basic (optional)'}),
            'brand': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Bambu Lab'}),
            'color': FilamentColorSelect(attrs={'class': 'form-select', 'id': 'id_color'}),
            'color_hex': forms.TextInput(attrs={
                'class': 'form-control',
                'type': 'color',
                'id': 'id_color_hex_picker'
            }),
            'diameter': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'initial_weight_grams': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '1000', 'min': '0'}),
            'remaining_percent': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'max': '100', 'step': '0.01'}),
            'remaining_weight_grams': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'step': '0.01'}),
            'is_transparent': forms.CheckboxInput(attrs={'class': 'form-check-input', 'id': 'id_is_transparent'}),
            'is_loaded_in_ams': forms.HiddenInput(),
            'is_loaded_externally': forms.HiddenInput(),
            'current_printer': forms.Select(attrs={'class': 'form-select', 'id': 'id_current_printer'}),
            'current_tray_id': forms.HiddenInput(),
            'ams_unit_id': forms.HiddenInput(),
            'ams_type': forms.HiddenInput(),
            'purchase_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'purchase_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'supplier': forms.TextInput(attrs={'class': 'form-control'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        ams_units = kwargs.pop('ams_units', None) or []
        self.ams_units = ams_units
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.color_hex:
            self.fields['color_hex_text'].initial = self.instance.color_hex
        if not self.is_bound and not self.instance.pk:
            self.fields['initial_weight_grams'].initial = 1000
            self.fields['remaining_percent'].initial = 100
            self.fields['remaining_weight_grams'].initial = 1000

        self.fields['filament_type'].queryset = FilamentType.objects.all()
        self.fields['filament_type'].empty_label = '--- Select Filament Type ---'
        self.fields['filament_type'].required = False
        self.fields['current_printer'].queryset = self.fields['current_printer'].queryset.filter(is_active=True)
        self.fields['current_printer'].empty_label = '--- Select Printer ---'
        self.fields['current_printer'].required = False

        self.fields['type'].required = False
        self.fields['sub_type'].required = False
        self.fields['brand'].required = False
        self.fields['ams_unit_id'].required = False
        self.fields['ams_type'].required = False
        self.fields['location_target'].choices = self._location_target_choices(ams_units)
        self.fields['location_tray_id'].choices = self._location_tray_choices(ams_units)
        self.fields['location_target'].initial = self._initial_location_target()
        self.fields['location_tray_id'].initial = (
            str(self.instance.current_tray_id)
            if self.instance and self.instance.current_tray_id is not None and not self.instance.is_loaded_externally
            else ''
        )

        self._populate_color_choices()

    def _initial_location_target(self):
        if not self.instance or not self.instance.pk:
            return ''
        if self.instance.is_loaded_externally:
            return 'external'
        if self.instance.is_loaded_in_ams and self.instance.ams_unit_id is not None:
            return str(self.instance.ams_unit_id)
        return ''

    def _location_target_choices(self, ams_units):
        choices = [('', '--- Select AMS Unit ---'), ('external', 'External Spool')]
        seen_units = set()

        for unit in ams_units:
            unit_id = unit.get('unit_id')
            printer_id = unit.get('printer_id')
            slot_key = (printer_id, unit_id)
            if unit_id is None or slot_key in seen_units:
                continue
            seen_units.add(slot_key)
            label = f"{unit.get('ams_type') or 'AMS'} (Unit {unit_id})"
            choices.append((str(unit_id), label))

        instance_key = (
            self.instance.current_printer_id if self.instance else None,
            self.instance.ams_unit_id if self.instance else None,
        )
        if (
            self.instance
            and self.instance.ams_unit_id is not None
            and instance_key not in seen_units
        ):
            label = self.instance.ams_type or 'AMS'
            choices.append((str(self.instance.ams_unit_id), f"{label} (Unit {self.instance.ams_unit_id})"))

        if len(choices) == 2:
            choices.append(('0', 'AMS (Unit 0)'))
        return choices

    def _location_tray_choices(self, ams_units):
        tray_ids = set()
        for unit in ams_units:
            tray_ids.update(unit.get('tray_ids') or [])
        if self.instance and self.instance.current_tray_id is not None:
            tray_ids.add(self.instance.current_tray_id)
        if not tray_ids:
            tray_ids.update(range(4))

        tray_choices = [('', '--- Select Tray ---')]
        tray_choices.extend((str(tray_id), f"Tray {tray_id}") for tray_id in sorted(tray_ids))
        return tray_choices

    def _populate_color_choices(self):
        """Populate color field choices from the global FilamentColor database."""
        from .utils import strip_color_padding, match_filament_color

        color_choices = [('', '--- Select Color ---')]
        option_attrs = [{}]
        suggested_color = None

        all_colors = FilamentColor.objects.all().order_by('finish', 'color_name')

        if self.instance and self.instance.type and self.instance.color_hex:
            color_code = strip_color_padding(self.instance.color_hex.lstrip('#'))
            suggested = match_filament_color(
                filament_type=self.instance.type,
                filament_sub_type=self.instance.sub_type,
                color_code=color_code,
                brand=self.instance.brand or 'Bambu Lab'
            )
            if suggested:
                suggested_color = suggested

        if suggested_color:
            color_choices.append((
                suggested_color.display_name,
                f"SUGGESTED: {suggested_color.display_name}"
            ))
            option_attrs.append(self._color_option_attrs(suggested_color))
            color_choices.append(('---separator---', '---' * 20))
            option_attrs.append({'disabled': 'disabled'})

        for color in all_colors:
            if suggested_color and color.pk == suggested_color.pk:
                continue

            color_choices.append((color.display_name, color.display_name))
            option_attrs.append(self._color_option_attrs(color))

        color_choices.append(('---separator2---', '---' * 20))
        option_attrs.append({'disabled': 'disabled'})
        color_choices.append(('custom', 'Custom (type in manually)'))
        option_attrs.append({})

        self.fields['color'].widget.choices = color_choices
        self.fields['color'].widget.option_attrs = option_attrs

    def _color_option_attrs(self, color):
        attrs = {
            'data-color-hex': color.get_hex_color(),
        }
        if color.is_transparent:
            attrs['data-color-transparent'] = 'true'
        return attrs

    def clean(self):
        cleaned_data = super().clean()
        is_loaded = cleaned_data.get('is_loaded_in_ams')
        is_external = cleaned_data.get('is_loaded_externally')
        current_printer = cleaned_data.get('current_printer')
        location_target = cleaned_data.get('location_target') or ''
        location_tray_id = cleaned_data.get('location_tray_id') or ''

        color_hex_text = cleaned_data.get('color_hex_text')
        if color_hex_text:
            cleaned_data['color_hex'] = color_hex_text

        self._sync_remaining_fields(cleaned_data)

        color = cleaned_data.get('color')
        if color and 'separator' in color:
            cleaned_data['color'] = ''

        ft = cleaned_data.get('filament_type')
        if ft:
            cleaned_data['type'] = ft.type
            cleaned_data['sub_type'] = ft.sub_type or ''
            cleaned_data['brand'] = ft.brand

        if current_printer is None:
            cleaned_data['is_loaded_in_ams'] = False
            cleaned_data['is_loaded_externally'] = False
            cleaned_data['current_tray_id'] = None
            cleaned_data['ams_unit_id'] = None
            cleaned_data['ams_type'] = ''
            return cleaned_data

        if location_target == 'external':
            cleaned_data['is_loaded_in_ams'] = False
            cleaned_data['is_loaded_externally'] = True
            cleaned_data['current_tray_id'] = 254
            cleaned_data['ams_unit_id'] = None
            cleaned_data['ams_type'] = ''
            return cleaned_data

        if not location_target:
            raise forms.ValidationError('AMS Unit or External Spool required when printer is selected')

        ams_unit_id = self._parse_location_int(location_target)
        tray_id = self._parse_location_int(location_tray_id)
        if ams_unit_id is None:
            raise forms.ValidationError('AMS Unit required when printer is selected')
        if tray_id is None:
            raise forms.ValidationError('AMS Tray ID required when AMS Unit is selected')

        cleaned_data['is_loaded_in_ams'] = True
        cleaned_data['is_loaded_externally'] = False
        cleaned_data['ams_unit_id'] = ams_unit_id
        cleaned_data['current_tray_id'] = tray_id
        cleaned_data['ams_type'] = self._ams_type_for_location(current_printer.pk, ams_unit_id)

        return cleaned_data

    def _parse_location_int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _ams_type_for_location(self, printer_id, ams_unit_id):
        for unit in self.ams_units:
            if (
                str(unit.get('printer_id')) == str(printer_id)
                and str(unit.get('unit_id')) == str(ams_unit_id)
            ):
                return unit.get('ams_type') or ''
        if self.instance and self.instance.ams_unit_id == ams_unit_id:
            return self.instance.ams_type or ''
        return ''

    def _sync_remaining_fields(self, cleaned_data):
        initial_weight = cleaned_data.get('initial_weight_grams')
        remaining_weight = cleaned_data.get('remaining_weight_grams')
        remaining_percent = cleaned_data.get('remaining_percent')

        if not self.instance.pk and initial_weight is None:
            initial_weight = 1000
            cleaned_data['initial_weight_grams'] = initial_weight

        if initial_weight in (None, 0):
            return

        source = cleaned_data.get('remaining_source')
        if source not in {'weight', 'percent', 'initial'}:
            if 'remaining_weight_grams' in self.changed_data and 'remaining_percent' not in self.changed_data:
                source = 'weight'
            else:
                source = 'percent'

        if source == 'weight' and remaining_weight is not None:
            bounded_weight = self._quantize_decimal(max(Decimal("0"), remaining_weight))
            cleaned_data['remaining_weight_grams'] = bounded_weight
            cleaned_data['remaining_percent'] = self._quantize_decimal(
                max(
                    Decimal("0"),
                    min(Decimal("100"), bounded_weight / Decimal(str(initial_weight)) * Decimal("100")),
                )
            )
            return

        if remaining_percent is None:
            remaining_percent = Decimal("100")
            cleaned_data['remaining_percent'] = remaining_percent

        bounded_percent = self._quantize_decimal(
            max(Decimal("0"), min(Decimal("100"), remaining_percent))
        )
        cleaned_data['remaining_percent'] = bounded_percent
        cleaned_data['remaining_weight_grams'] = self._quantize_decimal(
            Decimal(str(initial_weight)) * (bounded_percent / Decimal("100"))
        )

    def _quantize_decimal(self, value):
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def save(self, commit=True):
        filament = super().save(commit=False)

        if filament.is_loaded_in_ams:
            filament.is_loaded_externally = False
            if not filament.last_loaded_date or any(
                field in self.changed_data
                for field in ('is_loaded_in_ams', 'current_printer', 'current_tray_id', 'ams_unit_id', 'ams_type')
            ):
                filament.last_loaded_date = timezone.now()
        elif filament.is_loaded_externally:
            filament.current_tray_id = 254
            filament.ams_unit_id = None
            filament.ams_type = ''
            if not filament.last_loaded_date or any(
                field in self.changed_data
                for field in ('is_loaded_externally', 'current_printer')
            ):
                filament.last_loaded_date = timezone.now()
        else:
            filament.current_printer = None
            filament.current_tray_id = None
            filament.ams_unit_id = None
            filament.ams_type = ''

        if commit:
            filament.save()
            self.save_m2m()
            if filament.is_loaded_in_ams:
                occupants = Filament.objects.filter(
                    is_loaded_in_ams=True,
                    current_printer=filament.current_printer,
                    current_tray_id=filament.current_tray_id,
                ).exclude(pk=filament.pk)
                if filament.ams_unit_id is not None:
                    occupants = occupants.filter(ams_unit_id=filament.ams_unit_id)
                occupants.update(
                    is_loaded_in_ams=False,
                    current_printer=None,
                    current_tray_id=None,
                    ams_unit_id=None,
                    ams_type='',
                )
            elif filament.is_loaded_externally:
                Filament.objects.filter(
                    is_loaded_externally=True,
                    current_printer=filament.current_printer,
                ).exclude(pk=filament.pk).update(
                    is_loaded_externally=False,
                    current_printer=None,
                    current_tray_id=None,
                    ams_unit_id=None,
                    ams_type='',
                )

        return filament


class FilamentColorForm(forms.ModelForm):
    """Form for managing the global FilamentColor database."""

    color_code = forms.CharField(
        required=False,
        widget=forms.HiddenInput()
    )

    color_hex_input = forms.CharField(
        required=True,
        max_length=7,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': '#000000',
            'pattern': '#[0-9A-Fa-f]{6}',
        }),
        label='Color Hex Code'
    )

    class Meta:
        model = FilamentColor
        fields = ['color_code', 'color_name', 'finish']
        widgets = {
            'color_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Black, Orange'}),
            'finish': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.color_code:
            self.fields['color_hex_input'].initial = f"#{self.instance.color_code}"

        self.fields['finish'].choices = FILAMENT_COLOR_FINISH_CHOICES

    def clean(self):
        cleaned_data = super().clean()

        color_hex = cleaned_data.get('color_hex_input', '')
        if color_hex:
            color_code = color_hex.lstrip('#').upper()[:6]
            cleaned_data['color_code'] = color_code

        finish = cleaned_data.get('finish') or 'Default'
        cleaned_data['is_transparent'] = finish in {'Transparent', 'Translucent'}

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.is_transparent = instance.finish in {'Transparent', 'Translucent'}
        if commit:
            instance.save()
            self.save_m2m()
        return instance
