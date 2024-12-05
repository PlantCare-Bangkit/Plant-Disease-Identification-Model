import os
import json
import uuid
import logging
import io
from datetime import datetime

import numpy as np
import requests
import uvicorn
import gunicorn
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from tensorflow.keras.models import load_model
from tensorflow.keras.preprocessing import image
import google.generativeai as genai
from google.cloud import storage, firestore, secretmanager
import firebase_admin
from firebase_admin import credentials, firestore

# Initialize FastAPI app
app = FastAPI()

# Set up logging
logging.basicConfig(level=logging.INFO)

# Load models once when the application starts
mango_model = load_model('models/mango.h5')
tomato_model = load_model('models/tomato.h5')
chili_model = load_model('models/chili.h5')

# Class names for predictions
class_names = {
    'mango': ['Anthracnose', 'Bacterial Canker', 'Cutting Weevil', 'Die Back',
              'Gall Midge', 'Healthy', 'Powdery Mildew', 'Sooty Mould'],  
    'tomato': ['Bacterial_spot', 'Early_blight', 'Late_blight', 'Leaf_Mold',
               'Septoria_leaf_spot', 'Spider_mites', 'Target_Spot',
               'Tomato_Yellow_Leaf_Curl_Virus', 'Tomato_mosaic_virus', 'healthy'],
    'chili': ['Bacterial_spot', 'Healthy', 'Late_blight', 'Leaf_Mold']
}

# Initialize the Secret Manager client
client = secretmanager.SecretManagerServiceClient()

# Access the secret
secret_name = "GCP_SA_KEY" 
project_id = "plantcare-443106"  
secret_version = "latest"

# Build the resource name of the secret
name = f"projects/{project_id}/secrets/{secret_name}/versions/{secret_version}"

# Access the secret version
response = client.access_secret_version(name=name)

# Get the secret payload
service_account_info = response.payload.data.decode('UTF-8')

# Initialize Google Cloud Storage client
storage_client = storage.Client()
BUCKET_NAME = "plantcare-api-bucket"

# Initialize Firestore client
cred = credentials.Certificate(json.loads(service_account_info))
firebase_admin.initialize_app(cred)
db = firestore.client()

# Pydantic models for request and response
class TreatmentRequest(BaseModel):
    disease: str
    plant: str
    user_id: str

class PredictionResponse(BaseModel):
    user_id: str
    plant_type: str
    disease: str
    probability: float
    image_url: str
    treatment: str = None
    scanned_data: str  

@app.get("/")
async def read_root():
    logging.info("Received request at root endpoint")
    return {"message": "Hello, World!"}

@app.post('/predict/', response_model=PredictionResponse)
async def predict(file: UploadFile = File(...), plant_type: str = Form(...), user_id: str = Form(...)):
    """Predict the disease of a plant based on an uploaded image and automatically run treatment suggestion."""
    
    if plant_type not in class_names:
        raise HTTPException(status_code=400, detail='Invalid plant type')

    try:
        # Upload the image to Google Cloud Storage
        blob = storage_client.bucket(BUCKET_NAME).blob(f"{uuid.uuid4()}_{file.filename}")
        blob.upload_from_file(file.file)

        # Get the public URL of the uploaded image
        image_url = blob.public_url

        # Load the image for prediction
        img = image.load_img(io.BytesIO(requests.get(image_url).content), target_size=(256, 256))
        img_array = image.img_to_array(img) / 255.0
        img_array = np.expand_dims(img_array, axis=0)

        # Select the appropriate model
        model = mango_model if plant_type == 'mango' else tomato_model

        # Make predictions
        predictions = model.predict(img_array)
        predicted_class = np.argmax(predictions)
        disease_name = class_names[plant_type][predicted_class]

        # Create a unique document name using UUID
        document_id = str(uuid.uuid4())

        # Get the current timestamp and format it to YYYY:MM:DD
        scanned_data = datetime.now().strftime('%Y:%m:%d') 

        # Store the prediction result in Firestore
        result = {
            'user_id': user_id,
            'plant_type': plant_type,
            'disease': disease_name,
            'probability': float(predictions[0][predicted_class]),
            'image_url': image_url,
            'treatment': None,
            'scanned_data': scanned_data 
        }

        db.collection('predictions').document(document_id).set(result)

        treatment_text = await generate_treatment(disease_name, plant_type, user_id)
        result['treatment'] = treatment_text

        db.collection('predictions').document(document_id).update({'treatment': treatment_text})

        return JSONResponse(content=result)

    except Exception as e:
        logging.error(f"Error processing the image: {str(e)}")
        raise HTTPException(status_code=500, detail=f'Error processing the image: {str(e)}')

async def generate_treatment(disease: str, plant: str, user_id: str) -> str:
    """Generate treatment suggestions based on the disease and plant type."""
    genai.configure(api_key="AIzaSyCXrHQKYgn2VWxe3iGaxz7y55U9ogdJU3I")  
    model = genai.GenerativeModel("gemini-1.5-flash")

    prompt = f"Langkah-langkah mengatasi/merawat {plant} yang terkena penyakit {disease} dengan penjelasan singkat dan tepat"

    try:
        treatment_suggestion = model.generate_content(prompt)
        return treatment_suggestion.text if treatment_suggestion else "No suggestion available."
    except Exception as e:
        logging.error(f"Error generating treatment suggestion: {str(e)}")
        return "Error generating treatment suggestion."

@app.get('/scanned_data/')
async def get_scanned_data():
    """Fetch all predictions from the database."""
    predictions = db.collection('predictions').stream()
    results = []

    for doc in predictions:
        data = doc.to_dict()
        data['id'] = doc.id 
        results.append(data)

    return JSONResponse(content=results)

@app.get('/scanned_data/{user_id}', response_model=list[PredictionResponse])
async def get_prediction(user_id: str):
    """Fetch all predictions for a specific user ID."""
    try:
        predictions_ref = db.collection('predictions').where('user_id', '==', user_id).stream()
        results = []

        for doc in predictions_ref:
            prediction_data = doc.to_dict()
            prediction_data['id'] = doc.id 
            results.append(prediction_data)

        if not results:
            raise HTTPException(status_code=404, detail='No prediction data found for this user ID.')

        return JSONResponse(content=results)

    except Exception as e:
        logging.error(f"Error fetching predictions: {str(e)}")
        raise HTTPException(status_code=500, detail=f'Error fetching predictions: {str(e)}')

@app.delete('/scanned_data/{user_id}')
async def delete_prediction(user_id: str):
    """Delete all predictions for a specific user ID."""
    try:
        predictions_ref = db.collection('predictions').where('user_id', '==', user_id).stream()
        deleted_count = 0

        for doc in predictions_ref:
            doc.reference.delete() 
            deleted_count += 1

        if deleted_count == 0:
            raise HTTPException(status_code=404, detail='No prediction data found for this user ID.')

        return JSONResponse(content={'message': f'Deleted {deleted_count} prediction(s) successfully.'})

    except Exception as e:
        logging.error(f"Error deleting predictions: {str(e)}")
        raise HTTPException(status_code=500, detail=f'Error deleting predictions: {str(e)}')
    
uvicorn.run(app, host="127.0.0.1", port=8080)