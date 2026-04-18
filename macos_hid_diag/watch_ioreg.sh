#!/bin/bash
# Poll InputReportCount from IORegistry every 250ms
while true; do
    COUNT=$(ioreg -n "AppleUserHIDDevice" -r -d 1 2>/dev/null | grep InputReportCount | awk '{print $NF}')
    echo "$(date +%H:%M:%S.%3N) InputReportCount=$COUNT"
    sleep 0.25
done
