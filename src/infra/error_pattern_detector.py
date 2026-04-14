"""Machine learning-based error pattern detector."""
from __future__ import annotations

import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split


class ErrorPatternDetector:
    def __init__(self):
        self.error_logs = []
        self.model = RandomForestClassifier()

    def log_error(self, error_message):
        self.error_logs.append(error_message)
        self.update_model()

    def get_error_count(self):
        return len(self.error_logs)

    def update_model(self):
        # Convert error logs to a DataFrame
        df = pd.DataFrame(self.error_logs)
        # Assuming error logs have a 'timestamp' and 'error_type' column
        X = df[['timestamp', 'error_type']]
        y = df['is_critical']

        # Split the data into training and testing sets
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

        # Train the model
        self.model.fit(X_train, y_train)

    def predict_error(self, error_message):
        # Predict if the error is critical
        prediction = self.model.predict([error_message])
        return prediction[0]
