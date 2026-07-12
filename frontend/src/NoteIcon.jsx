export default function NoteIcon({ className = "h-4 w-4" }) {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="currentColor"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      <ellipse cx="3.5" cy="13" rx="2.5" ry="2" />
      <ellipse cx="10.5" cy="11" rx="2.5" ry="2" />
      <path d="M5 4.5 13 3v8h-1V4.2L6 5.3V13H5V4.5Z" />
    </svg>
  );
}
