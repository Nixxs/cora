import simpleaudio as sa
from pydub import AudioSegment
from io import BytesIO
import speech_recognition as sr
from corava.aws_services import get_polly_client
from corava.utilities import log_message, remove_code

def speak(text, config):
    polly_client = get_polly_client(config)
    polly_text = remove_code(text)
    response = polly_client.synthesize_speech(
        Engine='neural',
        VoiceId='Joanna',
        OutputFormat='pcm',
        Text=polly_text
    )

    # Amazon Polly returns a stream for PCM format, which we can play directly
    audio_stream = response['AudioStream']
    # Use BytesIO to convert the audio stream to an audio segment
    audio_segment = AudioSegment.from_file(
        BytesIO(audio_stream.read()), 
        format="pcm", 
        frame_rate=16000,
        channels=1,
        sample_width=2
    )
    # Convert audio segment to raw audio data
    raw_audio_data = audio_segment.raw_data
    # Play the audio using simpleaudio
    play_obj = sa.play_buffer(
        raw_audio_data, num_channels=1, 
        bytes_per_sample=audio_segment.sample_width, 
        sample_rate=audio_segment.frame_rate
    )

    log_message("CORA", f"{text}")
    # Wait for playback to finish before exiting
    play_obj.wait_done()

def listen(sleeping):
    recognizer = sr.Recognizer()
    with sr.Microphone() as source:
        if not(sleeping):
            print("Listening..", end="")
        audio = recognizer.listen(source)
        query = ""

        try:
            if not(sleeping):
                print("Recognizing..")
            query = recognizer.recognize_google(audio, language="en-AU")
            log_message("USER", query)
        except Exception as e:
            log_message("SYSTEM", "Sound detected but speech not recognized.")
 
    return query.lower()