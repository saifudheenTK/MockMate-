from flask import Flask, render_template, request, redirect, url_for, jsonify, session, copy_current_request_context
from resume_parser import extract_text_from_pdf
import os
import pyttsx3
import whisper
import speech_recognition as sr
import subprocess
import time
import threading
from flask_socketio import SocketIO

app = Flask(__name__)
app.secret_key = 'mockmate_secret_key'
socketio = SocketIO(app, async_mode='eventlet')

app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# 🗣️ Text-to-Speech
def speak(text, emit_socket=True):
    print("🤖 Bot says:", text)
    if emit_socket:
        socketio.emit('bot_message', text)
    engine = pyttsx3.init()
    voices = engine.getProperty('voices')
    engine.setProperty('voice', voices[0].id)
    engine.say(text)
    engine.runAndWait()

# 🎙️ Speech-to-Text
def record_and_transcribe():
    r = sr.Recognizer()
    with sr.Microphone() as source:
        print("🎤 Speak now...")
        socketio.emit('listening_status', {'status': 'active'})
        audio = r.listen(source, phrase_time_limit=10)
        socketio.emit('listening_status', {'status': 'inactive'})
        with open("input_audio.wav", "wb") as f:
            f.write(audio.get_wav_data())
    model = whisper.load_model("base")
    result = model.transcribe("input_audio.wav")
    transcribed_text = result["text"]
    socketio.emit('user_message', transcribed_text)
    return transcribed_text

# 🧠 Generate Interview Questions
def generate_questions(resume_text):
    prompt = f"""
You are an AI interviewer. Based on the following resume, generate exactly 3 smart and relevant technical interview questions. Format them like:

1. Question 1
2. Question 2
3. Question 3

Resume:
{resume_text[:1500]}
"""
    try:
        result = subprocess.run(
            ["ollama", "run", "llama3", prompt],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore"
        )
        output = result.stdout.strip()
        print("\n🧠 LLM Raw Output:\n", output)
        import re
        questions = re.findall(r'\d+\.\s+(.*)', output)
        if not questions:
            questions = [line.strip() for line in output.split("\n") if line.strip().startswith(("1.", "2.", "3."))]
        if not questions:
            questions = ["Sorry, I couldn't generate proper questions."]
        return questions
    except Exception as e:
        print("❌ Error generating questions:", e)
        return ["Sorry, an error occurred."]

# 🧠 Evaluate Answer
def evaluate_answer_feedback(question, answer):
    prompt = f"""
You are an interview evaluator.

Q: {question}
A: {answer}

Evaluate the answer. Reply only in this format:
Correct: [short praise or insight]
OR
Wrong: [why it's incorrect and a suggested better answer]
"""
    result = subprocess.run(["ollama", "run", "llama3", prompt],
                            capture_output=True, text=True, encoding="utf-8", errors="ignore")
    output = result.stdout.strip()
    print("🎯 Feedback:", output)
    socketio.emit('feedback', output)
    if output.lower().startswith("correct"):
        return output, True
    else:
        return output, False

# 🔁 Follow-up
def generate_follow_up(question, previous_answer):
    prompt = f"""
The candidate gave an incomplete or incorrect answer.

Original Question: {question}
Candidate's Answer: {previous_answer}

Ask 1 simple follow-up question to explore their understanding further.
"""
    result = subprocess.run(["ollama", "run", "llama3", prompt],
                            capture_output=True, text=True, encoding="utf-8", errors="ignore")
    return result.stdout.strip()

@app.route('/')
def landing():
    return render_template('landing.html')

@app.route('/upload', methods=['POST'])
def upload_resume():
    if 'resume' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part'})
    file = request.files['resume']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file'})
    if file and file.filename.endswith('.pdf'):
        filename = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
        file.save(filename)
        resume_text = extract_text_from_pdf(filename)
        questions = generate_questions(resume_text)
        if "Sorry" in questions[0]:
            return jsonify({'status': 'error', 'message': 'Could not generate questions from resume'})
        session['questions'] = questions
        return jsonify({'status': 'success', 'message': 'Resume uploaded successfully!'})
    return jsonify({'status': 'error', 'message': 'Invalid file format. Please upload a PDF'})

@app.route('/interview')
def interview():
    if 'questions' not in session:
        return redirect(url_for('landing'))
    return render_template('interview.html', questions=session['questions'])

@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

@socketio.on('start_interview')
def handle_start_interview():
    if 'questions' in session:
        questions = session['questions']
        welcome_msg = "Hello! I'm MockMate, your voice-based AI mock interviewer. I'll ask questions based on your resume. Please answer clearly after each one."
        socketio.emit('bot_message', welcome_msg)
        speak(welcome_msg, emit_socket=False)
        socketio.emit('current_question', {'question': questions[0], 'index': 0})
        socketio.emit('bot_message', questions[0])
        speak(questions[0], emit_socket=False)

@socketio.on('start_recording')
def handle_start_recording(data):
    current_idx = data.get('index', 0)
    # Get questions from session in the current request context
    questions = session.get('questions', [])
    
    @copy_current_request_context
    def process_recording(index, question_list):
        try:
            if index >= len(question_list):
                return
            current_question = question_list[index]
            user_answer = record_and_transcribe()
            feedback, is_correct = evaluate_answer_feedback(current_question, user_answer)
            speak(feedback, emit_socket=False)
            if not is_correct:
                followup = generate_follow_up(current_question, user_answer)
                socketio.emit('bot_message', "Here's a follow-up question.")
                socketio.emit('bot_message', followup)
                speak("Here's a follow-up question.", emit_socket=False)
                speak(followup, emit_socket=False)
                socketio.emit('await_followup', {'index': index})
            else:
                next_idx = index + 1
                if next_idx < len(question_list):
                    socketio.emit('current_question', {'question': question_list[next_idx], 'index': next_idx})
                    socketio.emit('bot_message', question_list[next_idx])
                    speak(question_list[next_idx], emit_socket=False)
                else:
                    completion_msg = "That concludes your mock interview. Thank you and all the best!"
                    socketio.emit('interview_complete')
                    socketio.emit('bot_message', completion_msg)
                    speak(completion_msg, emit_socket=False)
        except Exception as e:
            print(f"Error in recording thread: {e}")
            socketio.emit('bot_message', "Sorry, there was an error processing your answer. Please try again.")
    
    # Start the thread with the questions already retrieved from session
    threading.Thread(target=process_recording, args=(current_idx, questions)).start()
    return {'status': 'recording_started'}

@socketio.on('followup_response')
def handle_followup_response(data):
    current_idx = data.get('index', 0)
    # Get questions from session in the current request context
    questions = session.get('questions', [])
    
    @copy_current_request_context
    def process_followup(index, question_list):
        try:
            followup_ans = record_and_transcribe()
            socketio.emit('bot_message', "Thank you for your answer.")
            speak("Thank you for your answer.", emit_socket=False)
            
            next_idx = index + 1
            if next_idx < len(question_list):
                socketio.emit('current_question', {'question': question_list[next_idx], 'index': next_idx})
                socketio.emit('bot_message', question_list[next_idx])
                speak(question_list[next_idx], emit_socket=False)
            else:
                completion_msg = "That concludes your mock interview. Thank you and all the best!"
                socketio.emit('interview_complete')
                socketio.emit('bot_message', completion_msg)
                speak(completion_msg, emit_socket=False)
        except Exception as e:
            print(f"Error in followup thread: {e}")
            socketio.emit('bot_message', "Sorry, there was an error processing your answer. Please try again.")
    
    # Start the thread with the questions already retrieved from session
    threading.Thread(target=process_followup, args=(current_idx, questions)).start()
    return {'status': 'followup_started'}

# ✅ Run the app
if __name__ == '__main__':
    socketio.run(app, debug=True)