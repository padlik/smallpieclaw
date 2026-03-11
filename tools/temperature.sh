#!/bin/bash
# description: check CPU temperature of the Raspberry Pi

TEMP_FILE="/sys/class/thermal/thermal_zone0/temp"

if [ -f "$TEMP_FILE" ]; then
    RAW=$(cat "$TEMP_FILE")
    CELSIUS=$(echo "scale=1; $RAW / 1000" | bc 2>/dev/null || awk "BEGIN{printf \"%.1f\", $RAW/1000}")
    echo "CPU Temperature: ${CELSIUS}°C"
    # Warn if hot
    if awk "BEGIN{exit !($CELSIUS > 70)}"; then
        echo "⚠️  WARNING: Temperature above 70°C — consider improving cooling."
    elif awk "BEGIN{exit !($CELSIUS > 80)}"; then
        echo "🔥 CRITICAL: Temperature above 80°C!"
    else
        echo "✅ Temperature is within normal range."
    fi
else
    echo "Temperature sensor not found (not running on Raspberry Pi hardware?)."
    # Fallback: try vcgencmd
    if command -v vcgencmd &>/dev/null; then
        vcgencmd measure_temp
    fi
fi
