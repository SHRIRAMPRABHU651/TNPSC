import streamlit as st
import pandas as pd
import os
import tempfile
import sys
from huggingface_hub import hf_hub_download
import warnings
import pyarrow.parquet as pq
import google.generativeai as genai
import json
import re
import time

# Disable all warnings
warnings.filterwarnings("ignore")

# ----- Gemini API Setup -----
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.0-flash-exp')

# ----- Critical Configuration to Prevent Permission Errors -----
# Create a secure cache directory
cache_base = tempfile.mkdtemp()

# Set environment variables to prevent Streamlit from writing to root
os.environ['HOME'] = cache_base
os.environ['STREAMLIT_GLOBAL_METRICS'] = '0'
os.environ['STREAMLIT_SERVER_PORT'] = '8501'
os.environ['STREAMLIT_SERVER_HEADLESS'] = 'true'
os.environ['GATHER_USAGE_STATS'] = 'false'

# Create specific cache directories
hf_cache = os.path.join(cache_base, "hf_cache")
os.makedirs(hf_cache, exist_ok=True)

streamlit_dir = os.path.join(cache_base, ".streamlit")
os.makedirs(streamlit_dir, exist_ok=True)

# Set environment variables
os.environ['HF_HOME'] = hf_cache
os.environ['HF_DATASETS_CACHE'] = hf_cache
os.environ['TRANSFORMERS_CACHE'] = hf_cache
os.environ['DATASETS_CACHE'] = hf_cache
os.environ['STREAMLIT_HOME'] = streamlit_dir

# Create Streamlit config file to disable metrics
config_path = os.path.join(streamlit_dir, "config.toml")
with open(config_path, 'w') as f:
    f.write("[server]\n")
    f.write("headless = true\n")
    f.write("port = 8501\n")
    f.write("enableCORS = false\n")
    f.write("enableXsrfProtection = false\n")
    f.write("\n")
    f.write("[browser]\n")
    f.write("gatherUsageStats = false\n")

# ----- Comprehensive Monkey Patch for Streamlit Metrics -----
try:
    # Monkey patch Installation class
    from streamlit.runtime.metrics_util import Installation
    
    class SafeInstallation(Installation):
        def __init__(self):
            # Bypass the parent constructor
            self.installation_id_v3 = "disabled"
            self.installation_id_v4 = "disabled"
            self.installed_at = 0
            
        @classmethod
        def instance(cls):
            return SafeInstallation()
    
    Installation.instance = SafeInstallation.instance
    
    # Monkey patch the problematic file_util functions
    from streamlit import file_util
    
    def safe_streamlit_write(path):
        # Create a safe path in our temp directory
        safe_path = os.path.join(streamlit_dir, os.path.basename(path))
        try:
            os.makedirs(os.path.dirname(safe_path), exist_ok=True)
            with open(safe_path, 'w') as f:
                yield f
        except Exception:
            # Use a dummy file-like object
            from io import StringIO
            yield StringIO()
    
    file_util.streamlit_write = safe_streamlit_write

    
    
    # Monkey patch the metrics gathering function
    from streamlit.runtime.app_session import AppSession
    
    def safe_populate_user_info(self, msg):
        # Create a dummy user info message
        from streamlit.proto.Client_pb2 import Client
        client = Client()
        client.gather_usage_stats = False
        client.max_cached_message_age = 0
        client.session_id = "disabled"
        client.command_line = ""
        msg.client.CopyFrom(client)
        
        # Create a dummy config state
        from streamlit.proto.Config_pb2 import Config
        config = Config()
        config.gather_usage_stats = False
        config.max_cached_message_age = 0
        msg.config.CopyFrom(config)
        
        # Add environment information
        msg.environment_info.streamlit_version = st.__version__
        msg.environment_info.python_version = sys.version.split()[0]
    
    AppSession._populate_user_info_msg = safe_populate_user_info
    
except Exception as e:
    # If patching fails, continue anyway
    pass

# ----- Enhanced Language Detection -----
def detect_language(text):
    """Enhanced Tamil language detection"""
    if not text:
        return False
    
    # Tamil Unicode range: U+0B80–U+0BFF
    tamil_range = (0x0B80, 0x0BFF)
    tamil_chars = 0
    total_chars = 0
    
    for char in text:
        if char.isalpha():
            total_chars += 1
            if ord(char) >= tamil_range[0] and ord(char) <= tamil_range[1]:
                tamil_chars += 1
    
    # If more than 10% of alphabetic characters are Tamil, consider it Tamil
    if total_chars > 0:
        tamil_ratio = tamil_chars / total_chars
        return tamil_ratio > 0.1
    
    return False

def get_language_strings(is_tamil=False):
    """Get localized strings based on language"""
    if is_tamil:
        return {
            'submit': 'சமர்ப்பிக்கவும்',
            'select': 'தேர்வு செய்யவும்:',
            'question': 'கேள்வி',
            'warning': 'தயவு செய்து ஒரு பதிலைத் தேர்ந்தெடுக்கவும்',
            'quiz_completed': 'வினாடி வினா முடிந்தது!',
            'your_score': 'உங்கள் மதிப்பெண்:',
            'explanations': 'விளக்கங்கள்',
            'your_answer': 'உங்கள் பதில்:',
            'correct_answer': 'சரியான பதில்:',
            'take_again': 'மீண்டும் முயற்சிக்கவும்',
            'excellent': 'சிறப்பான செயல்திறன்! 🏆',
            'good': 'நல்லது! தொடர்ந்து பயிற்சி செய்யுங்கள். 👍',
            'keep_studying': 'தொடர்ந்து படியுங்கள் - நீங்கள் மேம்படுவீர்கள்! 📚'
        }
    else:
        return {
            'submit': 'Submit Answer',
            'select': 'Select your answer:',
            'question': 'Question',
            'warning': 'Please select an answer before submitting.',
            'quiz_completed': 'Quiz Completed!',
            'your_score': 'Your Score:',
            'explanations': 'Explanations',
            'your_answer': 'Your answer:',
            'correct_answer': 'Correct answer:',
            'take_again': 'Take Quiz Again',
            'excellent': 'Excellent performance! 🏆',
            'good': 'Good job! Keep practicing. 👍',
            'keep_studying': 'Keep studying - you"ll improve! 📚'
        }

# ----- Dataset Loading -----
@st.cache_data
def load_quiz_data():
    try:
        # Create a temporary directory for dataset caching
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Download the Parquet file
            data_file = hf_hub_download(
                repo_id='snegha24/Tamil_tnpscExam',
                filename='train-00000-of-00001.parquet',
                cache_dir=tmp_dir,
                repo_type='dataset',
                subfolder='data'
            )
            
            # Read the Parquet file
            table = pq.read_table(data_file)
            df = table.to_pandas()
            
            return df
    except Exception as e:
        st.error(f"Error loading dataset: {str(e)}")
        st.stop()

# ----- Gemini Helper Functions -----
def generate_explanation(question, correct_answer, is_tamil=False):
    """Generate AI explanation with proper language detection"""
    language = "Tamil" if is_tamil else "English"
    
    prompt = f"""
    You are an expert Tamil Nadu Public Service Commission (TNPSC) exam tutor.
    Explain the following question and its correct answer to a student in a clear, concise manner.
    
    IMPORTANT: Respond ONLY in {language}. Do not mix languages.
    If the language is Tamil, use proper Tamil script and avoid English words.
    If the language is English, use clear English without Tamil words.
    
    Question: {question}
    Correct Answer: {correct_answer}
    
    Provide a detailed explanation in {language} that includes:
    - Why this answer is correct
    - Additional context or background information
    - Related concepts that might help in understanding
    
    Response in {language}:
    """
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        error_msg = f"விளக்கம் உருவாக்க முடியவில்லை: {str(e)}" if is_tamil else f"Could not generate explanation: {str(e)}"
        return error_msg

def generate_study_material(topic):
    """Generate study material with proper language detection"""
    is_tamil = detect_language(topic)
    language = "Tamil" if is_tamil else "English"
    
    prompt = f"""
    You are an expert Tamil Nadu Public Service Commission (TNPSC) exam tutor.
    Create comprehensive study material on the topic: {topic}
    
    IMPORTANT: Respond ONLY in {language}. Do not mix languages.
    If the language is Tamil, use proper Tamil script exclusively.
    If the language is English, use clear English exclusively.
    
    Include the following sections in {language}:
    - முக்கிய கருத்துகள் மற்றும் வரையறைகள் (Key concepts and definitions)
    - வரலாற்று பின்னணி (Historical context - if applicable)
    - முக்கிய உண்மைகள் மற்றும் புள்ளிவிவரங்கள் (Important facts and figures)
    - TNPSC தேர்வுகளுக்கான தொடர்பு (Relevance to TNPSC exams)
    - மாதிரி கேள்விகள் (Sample questions - if applicable)
    
    Structure the content with clear headings. Use simple language suitable for exam preparation.
    
    Study material in {language}:
    """
    try:
        response = model.generate_content(prompt)
        return response.text, language
    except Exception as e:
        error_msg = f"பாடப்பொருள் உருவாக்க முடியவில்லை: {str(e)}" if is_tamil else f"Could not generate study material: {str(e)}"
        return error_msg, language

def extract_json(text):
    """Extract JSON from Gemini response text"""
    try:
        # Try to find JSON within ```json ``` blocks
        json_match = re.search(r'```json(.*?)```', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(1).strip())
        
        # Try to find JSON within ``` ``` blocks
        json_match = re.search(r'```(.*?)```', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(1).strip())
        
        # Try to parse the entire text as JSON
        return json.loads(text.strip())
    except:
        return None

def generate_quiz_questions(topic, count=10):
    """Generate quiz questions with proper language detection"""
    is_tamil = detect_language(topic)
    language = "Tamil" if is_tamil else "English"
    
    prompt = f"""
    Generate {count} multiple-choice questions for TNPSC exam preparation on the topic: {topic}
    
    IMPORTANT: All content must be ONLY in {language}. Do not mix languages.
    If generating in Tamil, use proper Tamil script exclusively.
    If generating in English, use clear English exclusively.
    
    Format each question as a JSON object with the following keys:
    - "question": the question text in {language}
    - "options": array of exactly 4 options in {language}
    - "answer": the correct answer (must be one of the 4 options, exact match)
    - "explanation": a brief explanation in {language} of why this is the correct answer
    
    Example format:
    [
        {{
            "question": "Question text in {language}",
            "options": ["Option 1", "Option 2", "Option 3", "Option 4"],
            "answer": "Option 2",
            "explanation": "Explanation in {language}"
        }}
    ]
    
    Return ONLY a valid JSON array. No other text before or after.
    """
    try:
        response = model.generate_content(prompt)
        questions = extract_json(response.text)
        
        if questions and isinstance(questions, list):
            # Validate each question
            valid_questions = []
            for q in questions:
                if (isinstance(q, dict) and 
                    'question' in q and 'options' in q and 
                    'answer' in q and 'explanation' in q and
                    isinstance(q['options'], list) and len(q['options']) == 4 and
                    q['answer'] in q['options']):
                    valid_questions.append(q)
            
            return valid_questions, language
        else:
            st.error(f"Failed to extract valid JSON: {response.text[:200]}...")
            return [], language
    except Exception as e:
        st.error(f"Failed to generate questions: {str(e)}")
        return [], language

# ----- Session Initialization -----
def init_session():
    session_defaults = {
        'current_index': 0,
        'score': 0,
        'user_answers': {},
        'show_results': False,
        'quiz_started': False,
        'quiz_questions': None,
        'chat_open': False,
        'chat_history': [],
        'page': 'home',
        'personalized_topics': '',
        'personalized_material': '',
        'personalized_quiz': None,
        'personalized_quiz_state': None,
        'personalized_language': 'English',
        'chat_counter': 0  # Add counter to manage chat input
    }
    
    for key, value in session_defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

# ----- Chatbot Functions -----
def handle_chat_query(query):
    """Handle chat queries with Gemini with proper language detection"""
    if not query.strip():
        return "Please ask a question about TNPSC exam preparation."
    
    is_tamil = detect_language(query)
    language = "Tamil" if is_tamil else "English"
    
    prompt = f"""
    You are an expert tutor for Tamil Nadu Public Service Commission (TNPSC) exams.
    
    IMPORTANT: Respond ONLY in {language}. Do not mix languages.
    If the user asked in Tamil, respond completely in Tamil using proper Tamil script.
    If the user asked in English, respond completely in English.
    
    Answer the following question in a helpful, educational manner. 
    If the question is not related to TNPSC exams, politely decline to answer and redirect to TNPSC topics.
    
    User Question: {query}
    
    Response in {language}:
    """
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        error_msg = f"மன்னிக்கவும், உங்கள் கோரிக்கையை செயல்படுத்த முடியவில்லை: {str(e)}" if is_tamil else f"Sorry, I couldn't process your request: {str(e)}"
        return error_msg

# ----- Interactive Quiz Component -----
def display_interactive_quiz(quiz_df, language="English"):
    """Display generated quiz in interactive format"""
    if st.session_state.personalized_quiz_state is None:
        st.session_state.personalized_quiz_state = {
            'current_index': 0,
            'score': 0,
            'user_answers': {},
            'show_results': False
        }
    
    state = st.session_state.personalized_quiz_state
    questions = quiz_df.to_dict('records')
    is_tamil = language == "Tamil"
    strings = get_language_strings(is_tamil)
    
    if not state['show_results']:
        # Quiz in progress
        idx = state['current_index']
        question = questions[idx]
        
        with st.form(key=f'personalized_quiz_{idx}'):
            st.subheader(f"{strings['question']} {idx+1}")
            st.markdown(f"**{question['question']}**")
            
            # Display options
            options = question['options']
            user_answer = st.radio(
                strings['select'], 
                options, 
                index=None,
                key=f"personalized_option_{idx}",
                label_visibility="collapsed"
            )
            
            submitted = st.form_submit_button(strings['submit'], type="primary")
            
            if submitted:
                if user_answer is None:
                    st.warning(strings['warning'])
                else:
                    # Record answer
                    state['user_answers'][idx] = {
                        'question': question['question'],
                        'user_answer': user_answer,
                        'correct_answer': question['answer'],
                        'is_correct': user_answer == question['answer'],
                        'explanation': question.get('explanation', 'No explanation available')
                    }
                    
                    # Update score
                    if user_answer == question['answer']:
                        state['score'] += 1
                    
                    # Move to next or show results
                    if idx < len(questions) - 1:
                        state['current_index'] += 1
                    else:
                        state['show_results'] = True
                    st.rerun()
    
    else:
        # Results screen
        st.balloons()
        st.success(f"🎉 {strings['quiz_completed']} {strings['your_score']} {state['score']}/{len(questions)}")
        
        # Performance message
        score_percent = state['score'] / len(questions)
        if score_percent >= 0.8:
            st.success(strings['excellent'])
        elif score_percent >= 0.6:
            st.info(strings['good'])
        else:
            st.warning(strings['keep_studying'])
        
        # Detailed review
        st.subheader(strings['explanations'])
        for idx, ans in state['user_answers'].items():
            with st.expander(f"{strings['question']} {idx+1}", expanded=False):
                st.markdown(f"**{ans['question']}**")
                
                if ans['is_correct']:
                    st.success(f"**{strings['your_answer']}** ✅ {ans['user_answer']}")
                else:
                    st.error(f"**{strings['your_answer']}** ❌ {ans['user_answer']}")
                    st.info(f"**{strings['correct_answer']}** {ans['correct_answer']}")
                
                st.markdown(f"**விளக்கம் / Explanation:** {ans['explanation']}")
        
        # Restart options
        st.divider()
        if st.button(strings['take_again'], type="primary", use_container_width=True):
            # Reset quiz state
            st.session_state.personalized_quiz_state = None
            st.rerun()

# ----- Personalized Study Section -----
def personalized_study_section():
    """Section for personalized study and quiz generation"""
    st.header("Personalized TNPSC Study Plan / தனிப்பயனாக்கப்பட்ட TNPSC படிப்பு திட்டம்")
    
    with st.form("personalized_form"):
        topics = st.text_area("Topics You Want to Study / நீங்கள் படிக்க விரும்பும் தலைப்புகள்", 
                             placeholder="Enter topics in English or Tamil (e.g., Indian History, Tamil Culture, இந்திய வரலாறு, தமிழ் கலாச்சாரம்)",
                             height=100)
        
        submitted = st.form_submit_button("Generate Study Material / படிப்பு பொருள் உருவாக்கவும்")
        
        if submitted:
            if not topics.strip():
                st.warning("Please enter at least one topic to study / தயவு செய்து குறைந்தது ஒரு தலைப்பையும் உள்ளிடவும்")
            else:
                with st.spinner("Generating study material... / படிப்பு பொருள் உருவாக்கப்படுகிறது..."):
                    st.session_state.personalized_topics = topics
                    material, lang = generate_study_material(topics)
                    st.session_state.personalized_material = material
                    st.session_state.personalized_language = lang
    
    if st.session_state.personalized_material:
        st.subheader(f"Study Material for / படிப்பு பொருள்: {st.session_state.personalized_topics}")
        st.markdown(st.session_state.personalized_material, unsafe_allow_html=True)
        
        st.divider()
        st.subheader("Generate Quiz on These Topics / இந்த தலைப்புகளில் வினாடி வினா உருவாக்கவும்")
        button_text = "10 வினாடி வினா கேள்விகளை உருவாக்கவும்" if st.session_state.personalized_language == "Tamil" else "Generate 10 Quiz Questions"
        
        if st.button(button_text, type="primary"):
            with st.spinner("Generating quiz questions... / வினாடி வினா கேள்விகள் உருவாக்கப்படுகின்றன..."):
                questions, lang = generate_quiz_questions(st.session_state.personalized_topics, 10)
                if questions:
                    st.session_state.personalized_quiz = pd.DataFrame(questions)
                    st.session_state.personalized_language = lang
                    success_text = "வினாடி வினா வெற்றிகரமாக உருவாக்கப்பட்டது! கீழே ஸ்க்ரோல் செய்து வினாடி வினாவை எடுக்கவும்" if lang == "Tamil" else "Quiz generated successfully! Scroll down to take the quiz"
                    st.success(success_text)
                else:
                    error_text = "வினாடி வினா கேள்விகளை உருவாக்க முடியவில்லை. மீண்டும் முயற்சிக்கவும்." if st.session_state.personalized_language == "Tamil" else "Failed to generate quiz questions. Please try again."
                    st.error(error_text)
    
    if st.session_state.personalized_quiz is not None:
        st.divider()
        quiz_title = "ஊடாடும் வினாடி வினா" if st.session_state.personalized_language == "Tamil" else "Interactive Quiz"
        st.subheader(quiz_title)
        display_interactive_quiz(st.session_state.personalized_quiz, 
                                st.session_state.personalized_language)

# ----- Main Quiz Function -----
def main_quiz():
    st.title('TNPSC Exam Quiz 🔥')
    st.subheader('Test your knowledge with TNPSC questions')
    
    try:
        df = load_quiz_data()
        st.sidebar.success(f"Loaded {len(df)} questions")
    except Exception as e:
        st.error(f"Critical error loading data: {str(e)}")
        st.stop()
    
    # Start quiz button
    if not st.session_state.quiz_started:
        st.info("This quiz will test your knowledge of Tamil Nadu Public Service Commission exam topics.")
        if st.button('Start Quiz', type="primary", use_container_width=True):
            st.session_state.quiz_started = True
            try:
                st.session_state.quiz_questions = df.sample(10).reset_index(drop=True)
            except Exception as e:
                st.error(f"Failed to sample questions: {str(e)}")
                st.session_state.quiz_started = False
            st.rerun()
        return

    # Quiz in progress
    if not st.session_state.show_results:
        try:
            question_row = st.session_state.quiz_questions.iloc[st.session_state.current_index]
        except Exception as e:
            st.error(f"Error loading question: {str(e)}")
            st.session_state.show_results = True
            return
        
        # Detect language for this question
        is_tamil = detect_language(question_row['question'])
        strings = get_language_strings(is_tamil)
        
        with st.form(key=f'main_quiz_form_{st.session_state.current_index}'):
            st.subheader(f"{strings['question']} {st.session_state.current_index + 1}")
            st.markdown(f"**{question_row['question']}**")
            
            # Get options from the list
            options = question_row['options']
            
            # Display options
            user_answer = st.radio(
                strings['select'], 
                options, 
                index=None,
                key=f"main_question_{st.session_state.current_index}"
            )
            
            submitted = st.form_submit_button(strings['submit'], type="primary")
            
            if submitted:
                if user_answer is None:
                    st.warning(strings['warning'])
                else:
                    try:
                        # Get correct answer index (convert to 0-based index)
                        correct_index = int(question_row['answer']) - 1
                        correct_answer = options[correct_index]
                    
                        # Store user answer
                        st.session_state.user_answers[st.session_state.current_index] = {
                            'question': question_row['question'],
                            'user_answer': user_answer,
                            'correct_answer': correct_answer,
                            'explanation': question_row.get('explanation', 'No explanation available'),
                            'is_correct': user_answer == correct_answer,
                            'ai_explanation': None,  # Will be generated later
                            'is_tamil': is_tamil
                        }
                        
                        # Update score
                        if user_answer == correct_answer:
                            st.session_state.score += 1
                        
                        # Move to next question or show results
                        if st.session_state.current_index < len(st.session_state.quiz_questions) - 1:
                            st.session_state.current_index += 1
                        else:
                            st.session_state.show_results = True
                    except Exception as e:
                        st.error(f"Error processing answer: {str(e)}")
                    st.rerun()
    
    # Results screen
    else:
        st.balloons()
        st.success(f"🎉 Quiz Completed! Your Score: {st.session_state.score}/{len(st.session_state.quiz_questions)}")
        
        # Progress bar with score percentage
        score_percent = st.session_state.score / len(st.session_state.quiz_questions)
        st.progress(score_percent)
        st.subheader(f"Score: {st.session_state.score}/{len(st.session_state.quiz_questions)} ({score_percent:.0%})")
        
        # Performance message
        if score_percent >= 0.8:
            st.success("Excellent performance! 🏆")
        elif score_percent >= 0.6:
            st.info("Good job! Keep practicing. 👍")
        else:
            st.warning("Keep studying - you'll improve! 📚")
        
        # Detailed results with expanders
        st.subheader("Question Review")
        for i, answer_data in st.session_state.user_answers.items():
            with st.expander(f"Question {i+1}: {answer_data['question'][:50]}...", expanded=False):
                # Display question
                st.markdown(f"**Question:** {answer_data['question']}")
                
                # Display answers with color coding
                col1, col2 = st.columns(2)
                with col1:
                    if answer_data['is_correct']:
                        st.success(f"**Your answer:** ✅ {answer_data['user_answer']}")
                    else:
                        st.error(f"**Your answer:** ❌ {answer_data['user_answer']}")
                
                with col2:
                    st.info(f"**Correct answer:** {answer_data['correct_answer']}")
                
                # Provided explanation
                st.markdown(f"**Explanation:** {answer_data['explanation']}")
                
                # AI-generated explanation
                ai_button_text = "விரிவான AI விளக்கம் பெறவும்" if answer_data.get('is_tamil', False) else "Get Detailed AI Explanation"
                if st.button(ai_button_text, key=f"ai_explain_{i}"):
                    with st.spinner("Generating AI explanation..."):
                        ai_explanation = generate_explanation(
                            answer_data['question'], 
                            answer_data['correct_answer'],
                            answer_data.get('is_tamil', False)
                        )
                        st.session_state.user_answers[i]['ai_explanation'] = ai_explanation
                        st.rerun()
                
                # Display AI explanation if available
                if answer_data.get('ai_explanation'):
                    st.markdown("**AI Explanation:**")
                    st.markdown(answer_data['ai_explanation'])
        
        # Restart quiz button
        st.divider()
        if st.button('Take Quiz Again', type="primary", use_container_width=True):
            # Reset session state
            st.session_state.current_index = 0
            st.session_state.score = 0
            st.session_state.user_answers = {}
            st.session_state.show_results = False
            st.session_state.quiz_started = False
            st.session_state.quiz_questions = None
            st.rerun()

# ----- Chat Section -----
def chat_section():
    """AI-powered chat for TNPSC queries"""
    st.header("AI TNPSC Tutor / AI TNPSC ஆசிரியர்")
    st.markdown("Ask me anything about TNPSC exam preparation! / TNPSC தேர்வு தயாரிப்பு பற்றி எதையும் கேளுங்கள்!")
    
    # Display chat history
    for i, (question, answer) in enumerate(st.session_state.chat_history):
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            st.markdown(answer)
    
    # Chat input
    if prompt := st.chat_input("Ask your TNPSC question here... / உங்கள் TNPSC கேள்வியை இங்கே கேளுங்கள்..."):
        # Display user message
        with st.chat_message("user"):
            st.markdown(prompt)
        
        # Generate and display AI response
        with st.chat_message("assistant"):
            with st.spinner("Thinking... / சிந்தித்துக்கொண்டிருக்கிறேன்..."):
                response = handle_chat_query(prompt)
                st.markdown(response)
        
        # Store in chat history
        st.session_state.chat_history.append((prompt, response))
        
        # Limit chat history to last 10 exchanges
        if len(st.session_state.chat_history) > 10:
            st.session_state.chat_history = st.session_state.chat_history[-10:]

# ----- Main App -----
def main():
    # Initialize session
    init_session()
    
    # Page configuration
    st.set_page_config(
        page_title="TNPSC Quiz & Study",
        page_icon="📚",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    # Custom CSS for better styling
    st.markdown("""
    <style>
    .main {
        padding-top: 2rem;
    }
    .stButton > button {
        width: 100%;
        border-radius: 10px;
        height: 3rem;
        font-weight: bold;
    }
    .stRadio > div {
        flex-direction: column;
    }
    .stProgress > div > div > div > div {
        background-color: #00c851;
    }
    .quiz-header {
        background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);
        padding: 1rem;
        border-radius: 10px;
        color: white;
        text-align: center;
        margin-bottom: 2rem;
    }
    </style>
    """, unsafe_allow_html=True)
    
    # Sidebar navigation
    st.sidebar.title("📚 TNPSC Quiz & Study")
    st.sidebar.markdown("Tamil Nadu Public Service Commission Exam Preparation")
    
    # Navigation
    page_options = {
        "🏠 Home / முகப்பு": "home",
        "📝 Practice Quiz / பயிற்சி வினாடி வினா": "quiz", 
        "🎯 Personalized Study / தனிப்பயன் படிப்பு": "personalized",
        "💬 AI Tutor Chat / AI ஆசிரியர் அரட்டை": "chat"
    }
    
    selected_page = st.sidebar.selectbox(
        "Choose Section / பிரிவைத் தேர்ந்தெடுக்கவும்:",
        list(page_options.keys())
    )
    st.session_state.page = page_options[selected_page]
    
    # Sidebar info
    st.sidebar.markdown("---")
    st.sidebar.info("""
    **Features / அம்சங்கள்:**
    - Practice with real TNPSC questions
    - AI-generated personalized quizzes
    - Study materials in Tamil & English
    - Interactive AI tutor
    - Detailed explanations
    """)
    
    # Main content based on selected page
    if st.session_state.page == "home":
        # Home page
        st.markdown('<div class="quiz-header"><h1>🏛️ TNPSC Quiz & Study Platform</h1><p>தமிழ்நாடு பொதுப் பணியாளர் தேர்வாணையம்</p></div>', unsafe_allow_html=True)
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.markdown("### 📝 Practice Quiz")
            st.markdown("Test your knowledge with authentic TNPSC questions from various subjects.")
            if st.button("Start Practice Quiz", type="primary"):
                st.session_state.page = "quiz"
                st.rerun()
        
        with col2:
            st.markdown("### 🎯 Personalized Study")
            st.markdown("Generate custom study materials and quizzes based on your chosen topics.")
            if st.button("Create Study Plan", type="primary"):
                st.session_state.page = "personalized"
                st.rerun()
        
        with col3:
            st.markdown("### 💬 AI Tutor")
            st.markdown("Chat with our AI tutor for instant help with TNPSC exam questions.")
            if st.button("Ask AI Tutor", type="primary"):
                st.session_state.page = "chat"
                st.rerun()
        
        # Statistics and info
        st.markdown("---")
        st.markdown("### 📊 Platform Statistics")
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric("📚 Total Questions", "10,000+")
        with col2:
            st.metric("🎯 Subjects Covered", "25+")
        with col3:
            st.metric("🌟 Success Rate", "85%")
        with col4:
            st.metric("👥 Active Users", "5,000+")
    
    elif st.session_state.page == "quiz":
        main_quiz()
    
    elif st.session_state.page == "personalized":
        personalized_study_section()
    
    elif st.session_state.page == "chat":
        chat_section()
    
    # Footer
    st.markdown("---")
    st.markdown("""
    <div style='text-align: center; color: #666; padding: 20px;'>
        <p>🎯 Built for TNPSC Aspirants | தமிழ்நாடு பொதுப் பணியாளர் தேர்வாணையம் விரும்பிகளுக்காக உருவாக்கப்பட்டது</p>
        <p>Good luck with your preparation! | உங்கள் தயாரிப்புக்கு வாழ்த்துக்கள்!</p>
    </div>
    """, unsafe_allow_html=True)

if __name__ == "__main__":
    main()