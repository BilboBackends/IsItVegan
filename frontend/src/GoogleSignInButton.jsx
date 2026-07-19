import { useEffect, useRef, useState } from "react";
import { renderGoogleSignInButton } from "./cloud.js";

// Google's own sign-in button (required by the GIS ID-token flow). Renders
// nothing when the GIS script can't load — ad blockers commonly block it —
// so the email magic-link path below it stays the visible fallback.
export default function GoogleSignInButton({ onError, className }) {
  const containerRef = useRef(null);
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    renderGoogleSignInButton(containerRef.current, {
      onError: (error) => {
        if (!cancelled) onErrorRef.current?.(error);
      },
    }).catch(() => {
      if (!cancelled) setFailed(true);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (failed) return null;
  return <div ref={containerRef} className={className} />;
}
