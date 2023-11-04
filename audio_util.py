import simpleaudio as sa
from pydub import AudioSegment
from io import BytesIO
import speech_recognition as sr
from aws_services import get_polly_client

def speak(text):
    polly_client = get_polly_client()

    response = polly_client.synthesize_speech(
        Engine='neural',
        VoiceId='Ruth',
        OutputFormat='pcm',
        Text=text
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

    print(f"CORA: {text}")
    # Wait for playback to finish before exiting
    play_obj.wait_done()

def listen():
    recognizer = sr.Recognizer()
    with sr.Microphone() as source:
        print("Listening..", end="")
        recognizer.adjust_for_ambient_noise(source)
        audio = recognizer.listen(source)
        query = ""

        try:
            print("Recognizing..", end="")
            query = recognizer.recognize_google(audio, language="en-AU")
            print(f"User said: {query}")
        except Exception as e:
            print(f"Sound detected but speech not recognized: {str(e)}")
 
    return query.lower()