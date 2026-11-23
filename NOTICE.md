# Notice

A project-wide redistribution license has not been selected for the RAVORA
case study. Repository visibility and a redistribution license are separate.

The RMS VAD adapter preserves the frame and noise-estimation method from the
author's [airband_ai VAD filter](https://github.com/yukimurata0421/airband_ai/blob/3cbfacca54554f4ae2229a23dcafbbb59e1be41a/scripts/vad_filter.py).
The [upstream readme](https://github.com/yukimurata0421/airband_ai/blob/3cbfacca54554f4ae2229a23dcafbbb59e1be41a/readme.md)
declares an MIT license.

The snapshot contains no original avatar, model weights, private recordings,
conversation database, credentials, or operational evidence bundle. It does not
redistribute third-party character-rendering bundles or the development RMS speech filter. The live studio uses the speech filter in the separately installed recognition library.

Optional libraries and SDKs are installed separately and retain their upstream
licenses. The local speech binding is reference code and does not bundle a speech
engine. The default replay uses a standard-library waveform fixture instead.

Review the project license and any asset-specific redistribution conditions before
publishing an open-source release. The dated
[local validation record](publication/validation.json) preserves the archive-stage
results; current repository and workflow state is available on the repository.
