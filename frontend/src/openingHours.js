const DAYS = [
  "Sunday",
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
];

// Every restaurant currently indexed is in Florida. Keep this explicit so a
// visitor in another time zone still sees the restaurant's local status.
export const RESTAURANT_TIME_ZONE = "America/New_York";

function normalizedText(value) {
  return String(value || "")
    .replace(/[\u2000-\u200b\u202f\u205f\u3000]/g, " ")
    .replace(/[–—−]/g, "-")
    .replace(/\s+/g, " ")
    .trim();
}

function zonedClock(date, timeZone) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    weekday: "long",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  const value = (type) => parts.find((part) => part.type === type)?.value;
  return {
    day: value("weekday"),
    minutes: Number(value("hour")) * 60 + Number(value("minute")),
  };
}

function clockMinutes(hour, minute, meridiem) {
  let value = Number(hour) % 12;
  if (meridiem === "PM") value += 12;
  return value * 60 + Number(minute || 0);
}

function chooseRange(start, end) {
  const startMeridiems = start.meridiem ? [start.meridiem] : ["AM", "PM"];
  const endMeridiems = end.meridiem ? [end.meridiem] : ["AM", "PM"];
  if (!start.meridiem && !end.meridiem) return null;

  const candidates = [];
  for (const startMeridiem of startMeridiems) {
    for (const endMeridiem of endMeridiems) {
      const startMinutes = clockMinutes(start.hour, start.minute, startMeridiem);
      const endMinutes = clockMinutes(end.hour, end.minute, endMeridiem);
      const duration = (endMinutes - startMinutes + 1_440) % 1_440 || 1_440;
      // Restaurant shifts longer than 18 hours are almost always the wrong
      // inference for Google's compact "12:00-9:00 PM" formatting.
      if (duration <= 18 * 60) {
        candidates.push({ start: startMinutes, end: endMinutes, duration });
      }
    }
  }
  candidates.sort((a, b) => a.duration - b.duration);
  return candidates[0] || null;
}

function parseDayLine(line) {
  const normalized = normalizedText(line);
  const separator = normalized.indexOf(":");
  if (separator < 0) return null;
  const day = normalized.slice(0, separator).trim();
  const hours = normalized.slice(separator + 1).trim();
  if (!DAYS.includes(day)) return null;
  if (/\bopen 24 hours\b/i.test(hours)) {
    return { day, text: hours, known: true, allDay: true, intervals: [] };
  }
  if (/\bclosed\b/i.test(hours)) {
    return { day, text: hours, known: true, allDay: false, intervals: [] };
  }

  const intervals = [];
  const rangePattern = /(\d{1,2})(?::(\d{2}))?\s*(AM|PM)?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*(AM|PM)?/gi;
  for (const match of hours.matchAll(rangePattern)) {
    const range = chooseRange(
      { hour: match[1], minute: match[2], meridiem: match[3]?.toUpperCase() },
      { hour: match[4], minute: match[5], meridiem: match[6]?.toUpperCase() }
    );
    if (range) intervals.push(range);
  }
  return { day, text: hours, known: intervals.length > 0, allDay: false, intervals };
}

function parsedSchedule(openingHours) {
  if (!Array.isArray(openingHours)) return new Map();
  return new Map(
    openingHours
      .map(parseDayLine)
      .filter(Boolean)
      .map((entry) => [entry.day, entry])
  );
}

export function todayHoursText(
  openingHours,
  date = new Date(),
  timeZone = RESTAURANT_TIME_ZONE
) {
  const { day } = zonedClock(date, timeZone);
  return parsedSchedule(openingHours).get(day)?.text || null;
}

export function scheduledOpenState(
  openingHours,
  date = new Date(),
  timeZone = RESTAURANT_TIME_ZONE
) {
  const schedule = parsedSchedule(openingHours);
  const { day, minutes } = zonedClock(date, timeZone);
  const dayIndex = DAYS.indexOf(day);
  const today = schedule.get(day);
  const yesterday = schedule.get(DAYS[(dayIndex + 6) % 7]);

  // An overnight Friday 5 PM-2 AM interval still controls Saturday at 1 AM.
  if (
    yesterday?.intervals.some(
      (interval) => interval.end <= interval.start && minutes < interval.end
    )
  ) {
    return true;
  }
  if (!today?.known) return null;
  if (today.allDay) return true;
  return today.intervals.some((interval) =>
    interval.end > interval.start
      ? minutes >= interval.start && minutes < interval.end
      : minutes >= interval.start
  );
}
