/** Decode a .json.gz response that was served as an ordinary static file. */
export async function readGzipJson(response) {
  // Some hosts attach Content-Encoding and Fetch transparently decompresses
  // the body. Others serve .gz as an opaque static file. Support both.
  if ((response.headers.get("content-encoding") || "").includes("gzip")) {
    return response.json();
  }
  if (!response.body) throw new Error("Compressed response has no body.");
  if (typeof DecompressionStream !== "function") {
    throw new Error("This browser cannot decompress gzip streams.");
  }
  const stream = response.body.pipeThrough(new DecompressionStream("gzip"));
  return new Response(stream).json();
}
