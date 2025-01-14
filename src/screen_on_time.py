#! /usr/bin/python3
"""
Copyright 2021 Andreas Finkler

This small script parses the log output of pmset to calculate how long the laptop was actively used since it
was last unplugged from the AC.
"""

import re
import subprocess
import sys
from bisect import bisect_left
from datetime import datetime

TIMESTAMP_REGEX = r"(?P<timestamp>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\s[+-]\d{4}"


def process_lines(pmset_lines):
    start_index = start_charge = start_timestamp = start_display_state = None
    charge_regex = re.compile(
        TIMESTAMP_REGEX
        + r"\s+\w+\s+.*Using (?P<type>AC|Batt|BATT)\s*\(Charge:\s*(?P<charge>\d+)%*\)"
    )
    display_regex = re.compile(
        TIMESTAMP_REGEX + r"\s+\w+\s+Display is turned (?P<state>\w+)"
    )

    seen_batt = False
    end_index = len(pmset_lines)
    for i, line in enumerate(reversed(pmset_lines)):
        match = charge_regex.match(line)
        if not match:
            continue
        charge_type = match["type"]

        # if we're currently on AC, we will report on usage statistics up until we plugged in
        if not seen_batt and charge_type == "AC":
            end_index = len(pmset_lines) - i - 1

        # if we've seen some battery entries we the AC event indicates the transition from plugged to unplugged
        if seen_batt and charge_type == "AC":
            break

        # remember the last battery record as it might be the battery event when unplugged
        if charge_type != "AC":
            start_index = len(pmset_lines) - i
            start_charge = int(match["charge"])
            start_timestamp = convert_timestamp(match["timestamp"])
            seen_batt = True
    else:
        print("Could not determine when the PC was last unplugged from AC.")
        sys.exit(1)

    for line in reversed(pmset_lines[:start_index]):
        match = display_regex.match(line)
        if match:
            start_display_state = match.groupdict()["state"]
            break
    else:
        print("Could not determine the state of the display when AC was unplugged.")
        sys.exit(1)

    charge_events = [
        (convert_timestamp(match[0]), int(match[-1]))
        for match in charge_regex.findall("\n".join(pmset_lines[start_index - 1:]))
    ]

    current_display_state = start_display_state
    last_display_switch = start_timestamp
    total_time_with_display_on = 0
    total_time_with_display_off = 0
    total_consumption_with_display_on = 0
    total_consumption_with_display_off = 0

    for line in pmset_lines[start_index:end_index]:
        display_match = display_regex.match(line)
        if display_match:
            groupdict = display_match.groupdict()
            new_display_state = groupdict["state"]
            if new_display_state == current_display_state:
                continue
            new_timestamp = convert_timestamp(groupdict["timestamp"])
            duration = (new_timestamp - last_display_switch).total_seconds()
            current_display_state = new_display_state
            consumption = (
                    get_closest_event(charge_events, last_display_switch)[1]
                    - get_closest_event(charge_events, new_timestamp)[1]
            )
            if duration > 300:
                # only emit messages for states lasting longer than a few minutes
                print(
                    "{} to {}: Used {:>3}% of battery during {:>3}h {:>2}min of {}".format(
                        last_display_switch.strftime("%Y-%m-%d %H:%M:%S"),
                        new_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                        consumption,
                        int(duration / 3600),
                        int(duration % 3600 / 60),
                        "sleep" if current_display_state == "on" else "usage",
                    )
                )
            if current_display_state == "on":
                total_consumption_with_display_off += consumption
                total_time_with_display_off += duration
            else:
                total_consumption_with_display_on += consumption
                total_time_with_display_on += duration
            last_display_switch = new_timestamp

    # if we're currently on battery , we need to measure time from last event
    if end_index == len(pmset_lines):
        # we assume that this script is only run manually, so the screen must be on now
        duration = (datetime.now() - last_display_switch).total_seconds()
        consumption = (
                get_closest_event(charge_events, last_display_switch)[1] - get_current_charge()
        )
        total_consumption_with_display_on += consumption
        total_time_with_display_on += duration
        print(
            "{} to {}: Used {:>3}% of battery during {:>3}h {:>2}min of usage".format(
                last_display_switch.strftime("%Y-%m-%d %H:%M:%S"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                consumption,
                int(duration / 3600),
                int(duration % 3600 / 60),
            )
        )

    # output summary
    print("\nSummary:")
    print(
        "Unplugged from AC on {} with {}% battery".format(
            start_timestamp.strftime("%Y-%m-%d %H:%M:%S"), start_charge
        )
    )
    print(
        "Used {:>3}% of battery during {:>3}h {:>2}min of active usage".format(
            total_consumption_with_display_on,
            int(total_time_with_display_on / 3600),
            int(total_time_with_display_on % 3600 / 60),
        )
    )
    print(
        "Used {:>3}% of battery during {:>3}h {:>2}min of sleep".format(
            total_consumption_with_display_off,
            int(total_time_with_display_off / 3600),
            int(total_time_with_display_off % 3600 / 60),
        )
    )

    # output statistics
    print("\nStatistics:")
    rate_with_display_on = 0.0
    if total_time_with_display_on > 0:
        rate_with_display_on = total_consumption_with_display_on / (total_time_with_display_on / 3600)
    print("{:.2f}%/h battery loss during usage".format(rate_with_display_on))
    rate_with_display_off = 0.0
    if total_time_with_display_off > 0:
        rate_with_display_off = total_consumption_with_display_off / (total_time_with_display_off / 3600)
    print("{:.2f}%/h battery loss during sleep".format(rate_with_display_off))


def main():
    if len(sys.argv) > 2:
        print(f"USAGE: {sys.argv[0]} [FILE]", file=sys.stderr)
        sys.exit(2)

    if len(sys.argv) == 1:
        pmset_lines = get_pmset_log().splitlines()
    else:
        # read log from a file
        with open(sys.argv[1], mode='r', encoding="utf-8") as f:
            pmset_lines = f.readlines()
    process_lines(pmset_lines)


def get_pmset_log():
    raw_output = subprocess.check_output(["pmset", "-g", "log"])
    return str(raw_output, "utf-8") if sys.version_info.major >= 3 else raw_output


def convert_timestamp(timestamp):
    return datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")


def get_closest_event(events, timestamp):
    """
    Get the event with the closest timestamp.
    """
    timestamps_of_events = [event[0] for event in events]
    pos = bisect_left(timestamps_of_events, timestamp)
    if pos == 0:
        return events[0]
    elif pos == len(events):
        return events[-1]
    else:
        before = events[pos - 1]
        after = events[pos]
        delta_to_before = (timestamp - before[0]).total_seconds()
        delta_to_after = (after[0] - timestamp).total_seconds()
        if min(delta_to_after, delta_to_before) > 600:
            print(
                "Next best charge info is {} minutes off".format(
                    min(delta_to_after, delta_to_before) // 60
                )
            )
        return before if delta_to_before < delta_to_after else after


def get_current_charge():
    for line in subprocess.check_output(
        ["ioreg", "-rn", "AppleSmartBattery"]
    ).splitlines():
        if b"CurrentCapacity" in line:
            return int(line.split()[-1])
    else:
        print("Could not read current battery capacity")
        sys.exit(1)


if __name__ == "__main__":
    main()
    sys.exit(0)
