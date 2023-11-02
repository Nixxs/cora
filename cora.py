import pyaudio
import pyttsx3
import speech_recognition as sr
import time
import env
import boto3
import simpleaudio as sa
from pydub import AudioSegment
from io import BytesIO

# Initialize a session using Amazon Polly
polly_client = boto3.client(
    'polly',
    aws_access_key_id=env.AWS_ACCESS_KEY,
    aws_secret_access_key=env.AWS_SECRET_KEY,
    region_name=env.AWS_REGION
)

def speak(text):
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

    # Wait for playback to finish before exiting
    play_obj.wait_done()

def listen():
    recognizer = sr.Recognizer()
    with sr.Microphone() as source:
        print("Listening..", end="")
        audio = recognizer.listen(source)
        query = ""

        try:
            print("Recognizing..", end="")
            query = recognizer.recognize_google(audio, language="en-AU")
            print(f"User said: {query}")
        except Exception as e:
            print(f"Exception: {str(e)}")
    return query.lower()

def main():
    talking = True
    while talking:
        responded = False
        userSaid = listen()
        if "hello" in userSaid:
            speak("hello")
            responded = True
        if "how are you" in userSaid:
            speak("doing fine, thanks")
            responded = True
        if "bye" in userSaid:
            talking = False
            speak("okay, see you next time")
            responded = True
        
        if not(responded):
            speak(f"I heard you say '{userSaid}' but I'm not sure how to respond to that.")

        time.sleep(2)
main()