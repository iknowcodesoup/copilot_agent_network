/*
 * Where each thing lives.
 *
 * Its own module so the observable layer and the query layer can both name a
 * base URL without importing each other - voice_api.ts announces writes into
 * voice_streams.ts, and voice_streams.ts needs the SSE origin.
 */

export const pythonApiUrl =
  process.env.NEXT_PUBLIC_PYTHON_API_URL ?? "http://localhost:8000";

/* What this service genuinely owns: run phase state, the AG-UI event stream,
   assignment and commit. */
export const voiceApiBase = `${pythonApiUrl}/api/voice`;

/*
 * Everything the voice factory owns - videos, clips, review decisions, clip
 * audio, characters, search - goes through one forwarder that adds nothing
 * (routes/voice_factory_proxy.py). There is no typed route per field in the
 * Python service, because nothing there reads these shapes: the factory
 * defines them and the browser consumes them.
 */
export const voiceFactoryBase = `${pythonApiUrl}/api/voice-factory`;

/*
 * The durable Voice entity lives under its own router (routes/voices.py,
 * plural), a sibling of the run-pipeline router (routes/voice.py, singular).
 * Same host, different path, so it needs its own base.
 */
export const voicesApiBase = `${pythonApiUrl}/api/voices`;
