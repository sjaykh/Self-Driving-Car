import asyncio
from aiohttp import ClientSession, ClientTimeout
from tornado import gen
import argparse
import cv2
from datetime import datetime
import time
import urllib.request
from ai.record_reader import RecordReader
import os
from os.path import dirname, join
import numpy as np
from psycopg2 import pool
import tornado.gen
import tornado.ioloop
import tornado.web
import tornado.websocket
import tornado.httpserver
import requests
import json
import signal
import subprocess
import threading
import shutil
from uuid import uuid4
from coordinator.utilities import *
import json
from shutil import rmtree
import traceback
from concurrent.futures import ThreadPoolExecutor
from ai.transformations import pseduo_crop, show_resize_effect
from coordinator.scheduler import Scheduler


class Home(tornado.web.RequestHandler):
    def get(self):
        self.render("dist/index.html")


class NewDatasetName(tornado.web.RequestHandler):
    executor = ThreadPoolExecutor(5)

    def get_next_id(self):
        sql_query = '''
            SELECT DISTINCT
              dataset
            FROM records
        '''
        rows = get_sql_rows(
            host=self.application.postgres_host,
            sql=sql_query,
            postgres_pool=self.application.postgres_pool
        )
        if len(rows) > 0:
            ids = []
            for row in rows:
                id = row['dataset'].split('_')[1]
                id = int(id)
                ids.append(id)
            return max(ids) + 1
        else:
            return 1

    def make_dataset_name(self, id):
        now = datetime.now()
        year = str(now.year)[2:]
        month = str(now.month)
        if len(month) == 1:
            month = '0' + month
        day = str(now.day)
        if len(day) == 1:
            day = '0' + day
        name = 'dataset_{id}_{year}-{month}-{day}'.format(
            id=id,
            year=year,
            month=month,
            day=day
        )
        return name

    @tornado.concurrent.run_on_executor
    def new_dataset_name(self):

        id = self.get_next_id()
        dataset_name = self.make_dataset_name(id)
        return {'name':dataset_name}

    @tornado.gen.coroutine
    def post(self):
        result = yield self.new_dataset_name()
        self.write(result)


class DeploymentHealth(tornado.web.RequestHandler):
    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def get_deployment_health(self,json_input):
        device = json_input['device']
        if device.lower() == 'laptop':
            host = 'localhost'
        elif device.lower() == 'pi':
            host = read_pi_setting(
                host=self.application.postgres_host,
                field_name='hostname'
            )
        else:
            pass
        seconds = 1
        try:
            request = requests.post(
                # TODO: Remove hardcoded port
                'http://{host}:8885/model-metadata'.format(host=host),
                timeout=seconds
            )
            response = json.loads(request.text)
            response['is_alive'] = True
            return response
        except:
            return {'is_alive': False}

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.get_deployment_health(json_input)
        self.write(result)


class ListModels(tornado.web.RequestHandler):
    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def list_models(self):
        sql_query = '''
            SELECT
              model_id,
              to_char(created_timestamp, 'YYYY-MM-DD HH24:MI:SS') AS created_timestamp,
              crop,
              '1/' || scale AS scale
            FROM models
            ORDER BY created_timestamp ASC
        '''
        rows = get_sql_rows(
            host=self.application.postgres_host,
            sql=sql_query,
            postgres_pool=self.application.postgres_pool
        )
        result = {'models':rows}
        return result

    @tornado.gen.coroutine
    def post(self):
        result = yield self.list_models()
        self.write(result)


class Memory(tornado.web.RequestHandler):
    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def get_memory(self):
        seconds = 3.0
        # TODO: Remove hardcoded port
        endpoint = 'http://{host}:{port}/output'.format(
           host=self.application.scheduler.service_host,
           port=self.application.scheduler.get_services()['memory']['port']
        )
        request = requests.get(
           endpoint,
           timeout=seconds
        )
        response = json.loads(request.text)
        return response

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.get_memory()
        self.write(result)


class ReadSlider(tornado.web.RequestHandler):
    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def read_slider(self, json_input):
        web_page = json_input['web_page']
        name = json_input['name']
        result = {}
        sql_query = '''
            SELECT
              amount
            FROM sliders
            WHERE LOWER(web_page) LIKE '%{web_page}%'
              AND LOWER(name) LIKE '%{name}%'
            ORDER BY event_ts DESC
            LIMIT 1;
        '''.format(
            web_page=web_page,
            name=name
        )
        rows = get_sql_rows(
            host=self.application.postgres_host,
            sql=sql_query,
            postgres_pool=self.application.postgres_pool
        )
        if len(rows) > 0:
            first_row = rows[0]
            amount = first_row['amount']
            result['amount'] = amount
        else:
            result['amount'] = None
        return result

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.read_slider(json_input=json_input)
        self.write(result)


class WriteSlider(tornado.web.RequestHandler):
    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def write_slider(self, json_input):
        web_page = json_input['web_page']
        name = json_input['name']
        amount = json_input['amount']
        sql_query = '''
            BEGIN;
            INSERT INTO sliders (
                event_ts,
                web_page,
                name,
                amount
            )
            VALUES (
                NOW(),
               '{web_page}',
               '{name}',
                {amount}
            );
            COMMIT;
        '''.format(
            web_page=web_page,
            name=name,
            amount=amount
        )
        execute_sql(
            host=self.application.postgres_host,
            sql=sql_query,
            postgres_pool=self.application.postgres_pool
        )
        return {}

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.write_slider(json_input=json_input)
        self.write(result)


class ListModelDeployments(tornado.web.RequestHandler):
    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def get_deployments(self):
        result = {}
        devices = ['laptop','pi']
        for device in devices:
            sql_query = '''
                WITH latest AS (
                  SELECT
                    model_id,
                    epoch_id,
                    ROW_NUMBER() OVER(PARTITION BY device ORDER BY event_ts DESC) AS latest_rank
                  FROM deployments
                  WHERE LOWER(device) LIKE LOWER('%{device}%')
                )
                SELECT
                  model_id,
                  epoch_id
                FROM latest
                WHERE latest_rank = 1
            '''.format(
                device=device
            )
            rows = get_sql_rows(
                host=self.application.postgres_host,
                sql=sql_query,
                postgres_pool=self.application.postgres_pool
            )
            if len(rows) > 0:
                first_row = rows[0]
                metadata = {
                    'model_id':first_row['model_id'],
                    'epoch_id':first_row['epoch_id']
                }
                result[device] = metadata
            else:
                metadata = {
                    'model_id': 'N/A',
                    'epoch_id': 'N/A'
                }
                result[device] = metadata
        return result

    @tornado.gen.coroutine
    def post(self):
        result = yield self.get_deployments()
        self.write(result)



class ReadToggle(tornado.web.RequestHandler):
    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def read_toggle(self, json_input):
        web_page = json_input['web_page']
        name = json_input['name']
        detail = json_input['detail']
        result = {}
        key = f'{web_page}-{detail}-{name}'
        if key in self.application.scheduler.toggles:
            result['is_on'] = self.application.scheduler.toggles[key]
        else:
            result['is_on'] = False
        return result

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.read_toggle(json_input=json_input)
        self.write(result)

class WriteToggle(tornado.web.RequestHandler):
    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def write_toggle(self, json_input):
        web_page = json_input['web_page']
        name = json_input['name']
        detail = json_input['detail']
        is_on = json_input['is_on']
        sql_query = '''
            BEGIN;
            INSERT INTO toggles (
                event_ts,
                web_page,
                name,
                detail,
                is_on
            )
            VALUES (
                NOW(),
               '{web_page}',
               '{name}',
               '{detail}',
                {is_on}
            );
            COMMIT;
        '''.format(
            web_page=web_page,
            name=name,
            detail=detail,
            is_on=is_on
        )
        execute_sql(
            host=self.application.postgres_host,
            sql=sql_query,
            postgres_pool=self.application.postgres_pool
        )
        return {}

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.write_toggle(json_input=json_input)
        self.write(result)

class WritePiField(tornado.web.RequestHandler):
    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def write_pi_field(self, json_input):
        column_name = json_input['column_name']
        column_value = json_input['column_value']
        sql_query = '''
            BEGIN;
            INSERT INTO pi_settings(
              event_ts,
              field_name,
              field_value
            )
            VALUES
              (now(), '{column_name}', '{column_value}');
            COMMIT;
        '''.format(
            column_name=column_name.lower(),
            column_value=column_value
        )
        execute_sql(
            host=self.application.postgres_host,
            sql=sql_query,
            postgres_pool=self.application.postgres_pool
        )
        return {}

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.write_pi_field(json_input=json_input)
        self.write(result)

class ReadPiField(tornado.web.RequestHandler):
    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def read_pi_field(self, json_input):
        column_name = json_input['column_name']
        result = {
            'column_value': self.application.scheduler.pi_settings[column_name]
        }
        return result

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.read_pi_field(json_input=json_input)
        self.write(result)

# Makes a copy of record for model to focus on this record
class Keep(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def keep(self,json_input):
        dataset_name = json_input['dataset']
        record_id = json_input['record_id']
        self.application.record_reader.write_flag(
            dataset=dataset_name,
            record_id=record_id,
            is_flagged=True
        )
        return {}

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        yield self.keep(json_input=json_input)


class DatasetRecordIdsAPIFileSystem(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def get_record_ids(self,json_input):
        dataset_name = json_input['dataset']
        dataset_type = json_input['dataset_type']
        if dataset_type.lower() in ['import', 'review', 'flagged']:
            if dataset_type.lower() == 'import':
                # TODO: Change to point to real import datasets
                path_id_pairs = self.application.record_reader.get_dataset_record_ids_filesystem(dataset_name)
            elif dataset_type.lower() == 'review':
                path_id_pairs = self.application.record_reader.get_dataset_record_ids_filesystem(dataset_name)
            elif dataset_type.lower() == 'flagged':
                path_id_pairs = self.application.record_reader.get_dataset_record_ids_filesystem(dataset_name)
            else:
                print('Unknown dataset_type: ' + dataset_type)
            record_ids = []
            for pair in path_id_pairs:
                path, record_id = pair
                record_ids.append(record_id)
            result = {
                'record_ids': record_ids
            }
            return result
        elif dataset_type.lower() == 'critical-errors':
            record_ids = []
            sql_query = '''
                DROP TABLE IF EXISTS latest_deployment;
                CREATE TEMP TABLE latest_deployment AS (
                  SELECT
                    model_id,
                    epoch
                  FROM predictions
                  ORDER BY created_timestamp DESC
                  LIMIT 1
                );

                SELECT
                  records.record_id
                FROM records
                LEFT JOIN predictions
                  ON records.dataset = predictions.dataset
                    AND records.record_id = predictions.record_id
                LEFT JOIN latest_deployment AS deploy
                  ON predictions.model_id = deploy.model_id
                    AND predictions.epoch = deploy.epoch
                WHERE LOWER(records.dataset) LIKE LOWER('%{dataset}%')
                  AND ABS(records.angle - predictions.angle) >= 0.8
                ORDER BY record_id ASC
                '''.format(dataset=dataset_name)
            rows = get_sql_rows(
                host=self.application.postgres_host,
                sql=sql_query,
                postgres_pool=self.application.postgres_pool
            )
            for row in rows:
                record_id = row['record_id']
                record_ids.append(record_id)
            result = {
                'record_ids': record_ids
            }
            return result
        else:
            print('Unknown dataset_type: ' + dataset_type)

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.get_record_ids(json_input=json_input)
        self.write(result)

class DatasetRecordIdsAPI(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def get_record_ids(self,json_input):
        dataset_name = json_input['dataset']
        dataset_type = json_input['dataset_type']
        if dataset_type.lower() in ['import', 'review', 'flagged']:
            if dataset_type.lower() == 'import':
                # TODO: Change to point to real import datasets
                ids = self.application.record_reader.get_dataset_record_ids(dataset_name)
            elif dataset_type.lower() == 'review':
                ids = self.application.record_reader.get_dataset_record_ids(dataset_name)
            elif dataset_type.lower() == 'flagged':
                ids = self.application.record_reader.get_dataset_record_ids(dataset_name)
            else:
                print('Unknown dataset_type: ' + dataset_type)
            record_ids = []
            for record_id in ids:
                record_ids.append(record_id)
            result = {
                'record_ids': record_ids
            }
            return result
        elif dataset_type.lower() == 'critical-errors':
            record_ids = []
            sql_query = '''
                DROP TABLE IF EXISTS latest_deployment;
                CREATE TEMP TABLE latest_deployment AS (
                  SELECT
                    model_id,
                    epoch
                  FROM predictions
                  ORDER BY created_timestamp DESC
                  LIMIT 1
                );

                SELECT
                  records.record_id
                FROM records
                LEFT JOIN predictions
                  ON records.dataset = predictions.dataset
                    AND records.record_id = predictions.record_id
                LEFT JOIN latest_deployment AS deploy
                  ON predictions.model_id = deploy.model_id
                    AND predictions.epoch = deploy.epoch
                WHERE LOWER(records.dataset) LIKE LOWER('%{dataset}%')
                  AND ABS(records.angle - predictions.angle) >= 0.8
                ORDER BY record_id ASC
                '''.format(dataset=dataset_name)
            rows = get_sql_rows(
                host=self.application.postgres_host,
                sql=sql_query,
                postgres_pool=self.application.postgres_pool
            )
            for row in rows:
                record_id = row['record_id']
                record_ids.append(record_id)
            result = {
                'record_ids': record_ids
            }
            return result
        else:
            print('Unknown dataset_type: ' + dataset_type)

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.get_record_ids(json_input=json_input)
        self.write(result)

class SaveRecordToDB(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def save_record_to_db(self, json_input):
        try:
            dataset_name = json_input['dataset']
            record_id = json_input['record_id']
            label_path = self.application.record_reader.get_label_path(
                dataset_name=dataset_name,
                record_id=record_id
            )
            image_path = self.application.record_reader.get_image_path_from_db(
                dataset_name=dataset_name,
                record_id=record_id
            )
            _, angle, throttle = self.application.record_reader.read_record(
                label_path=label_path
            )
            sql_query = '''
                BEGIN;
                INSERT INTO records (
                    dataset,
                    record_id,
                    label_path,
                    image_path,
                    angle,
                    throttle
                )
                VALUES (
                   '{dataset}',
                    {record_id},
                   '{label_path}',
                   '{image_path}',
                    {angle},
                    {throttle}
                );
                COMMIT;
            '''.format(
                dataset=dataset_name,
                record_id=record_id,
                label_path=label_path,
                image_path=image_path,
                angle=angle,
                throttle=throttle
            )
            execute_sql(
                host=self.application.postgres_host,
                sql=sql_query,
                postgres_pool=self.application.postgres_pool
            )
            return {}
        except:
            print(json_input)
            traceback.print_exc()

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        _ = self.save_record_to_db(json_input=json_input)
        self.write({})

class IsRecordAlreadyFlagged(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def is_record_already_flagged(self,json_input):
        dataset_name = json_input['dataset']
        record_id = json_input['record_id']
        is_flagged = self.application.record_reader.read_flag(
            dataset=dataset_name,
            record_id=record_id
        )
        result = {
            'is_already_flagged': is_flagged
        }
        return result

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.is_record_already_flagged(json_input=json_input)
        self.write(result)


class LaptopModelAPIHealth(tornado.web.RequestHandler):

    """
    Used to check if the laptop model Docker container is up. If it's
    not then I tell the drive modal to not show the model predictions
    in the javascript code. This will occur when you first start the project
    because there won't be a trained model to deploy
    """

    # Prevents awful blocking
    # https://infinitescript.com/2017/06/making-requests-non-blocking-in-tornado/
    executor = ThreadPoolExecutor(100)

    @tornado.concurrent.run_on_executor
    def get_health(self):
        try:
            timeout_seconds = 1
            request = requests.get('http://localhost:8886/health',timeout=timeout_seconds)
            response = json.loads(request.text)
            return {'is_healthy': response['is_healthy']}
        except:
            return {'is_healthy': False}

    @tornado.gen.coroutine
    def get(self):
        result = yield self.get_health()
        self.write(result)


class DeployModel(tornado.web.RequestHandler):

    async def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        device = json_input['device']
        await start_model_service(
            pi_hostname=self.application.scheduler.pi_hostname,
            pi_username=self.application.scheduler.pi_username,
            pi_password=self.application.scheduler.pi_password,
            host_port = self.application.scheduler.get_services()['angle-model-pi']['port'],
            device=device,
            session_id=self.application.session_id,
            aiopg_pool=self.application.scheduler.aiopg_pool
        )
        self.write({})


# Given a dataset name and record ID, return the user
# angle and throttle
class UserLabelsAPI(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    def get_label_path(self, dataset_name, record_id):
        sql = f"""
            SELECT
                label_path
            FROM records
            WHERE
                dataset = '{dataset_name}'
                AND record_id = {record_id}
        """
        rows = get_sql_rows(host=None, sql=sql, postgres_pool=self.application.postgres_pool)
        if len(rows) > 0:
            return rows[0]['label_path']
        else:
            return None

    @tornado.concurrent.run_on_executor
    def get_user_babels(self,json_input):
        dataset_name = json_input['dataset']
        record_id = int(json_input['record_id'])
        label_file_path = self.get_label_path(
            dataset_name=dataset_name,
            record_id=record_id
        )
        _, angle, throttle = self.application.record_reader.read_record(
            label_path=label_file_path)
        result = {
            'angle': angle,
            'throttle': throttle
        }
        return result

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.get_user_babels(json_input=json_input)
        self.write(result)

# This API might seem redundant given that I already have
# a separate API for producing predictions, but UI's version
# of the image has already been compressed once. Sending the
# compressed image to the model API would compress the image
# again (compression happens each time image is transferred)
# and this would lead to slightly different results vs if
# the image file is passed just once, between this API and
# the model API
class AIAngleAPI(tornado.web.RequestHandler):

    # Prevents awful blocking
    # https://infinitescript.com/2017/06/making-requests-non-blocking-in-tornado/
    executor = ThreadPoolExecutor(100)

    @tornado.concurrent.run_on_executor
    def get_prediction(self, json_input):
        dataset_name = json_input['dataset']
        record_id = json_input['record_id']

        frame = self.application.record_reader.get_image(
            dataset_name=dataset_name,
            record_id=record_id
        )

        img = cv2.imencode('.jpg', frame)[1].tostring()
        files = {'image': img}
        # TODO: Remove hard-coded model API
        request = requests.post('http://localhost:8886/predict', files=files)
        response = json.loads(request.text)
        prediction = response['prediction']
        predicted_angle = prediction
        result = {
            'angle': predicted_angle
        }
        return result

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.get_prediction(json_input)
        self.write(result)


class UpdateDeploymentsTable(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def update_deployments_table(self,json_input):
        device = json_input['device']
        model_id = json_input['model_id']

        sql_epoch_query = '''
            SELECT
              max(epoch) AS epoch_id
            FROM epochs WHERE model_id = {model_id}
        '''.format(
            model_id=model_id
        )
        epoch_id = get_sql_rows(
            host=self.application.postgres_host,
            sql=sql_epoch_query,
            postgres_pool=self.application.postgres_pool
        )[0]['epoch_id']

        insert_deployment_record_sql = """
            BEGIN;
            INSERT INTO deployments (
                device,
                model_id,
                epoch_id,
                event_ts
            ) VALUES (
                '{device}',
                 {model_id},
                 {epoch_id},
                 NOW()
            );
            COMMIT;
        """.format(
            device=device,
            model_id=model_id,
            epoch_id=epoch_id
        )
        execute_sql(
            host=self.application.postgres_host,
            sql=insert_deployment_record_sql,
            postgres_pool=self.application.postgres_pool
        )

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        yield self.update_deployments_table(json_input=json_input)


class DeleteModel(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def delete_model(self,json_input):
        model_id = json_input['model_id']
        # Delete the model folder and files
        base_model_directory = read_pi_setting(
            host=self.application.postgres_host,
            field_name='models_location_laptop',
            postgres_pool=self.application.postgres_pool
        )
        full_path = os.path.join(base_model_directory,str(model_id))
        rmtree(full_path)
        # Delete the model from the table
        delete_records_sql = """
            BEGIN;
            DELETE FROM models
            WHERE model_id = {model_id};

            DELETE FROm epochs
            WHERE model_id = {model_id};
            COMMIT;
        """.format(
            model_id=model_id
        )
        execute_sql(
            host=self.application.postgres_host,
            sql=delete_records_sql,
            postgres_pool=self.application.postgres_pool
        )

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        yield self.delete_model(json_input=json_input)


class DeleteRecord(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def delete_record(self,json_input):
        dataset_name = json_input['dataset']
        record_id = json_input['record_id']
        label_path = get_label_path_from_db(
            dataset_name=dataset_name,
            record_id=record_id,
            postgres_pool=self.application.postgres_pool
        )
        image_path = self.application.record_reader.get_image_path_from_db(
            dataset_name=dataset_name,
            record_id=record_id
        )
        delete_records_sql = """
            BEGIN;
            DELETE FROM records
            WHERE record_id = {record_id}
              AND LOWER(dataset) LIKE '{dataset}';
            COMMIT;
        """.format(
            record_id=record_id,
            dataset=dataset_name
        )
        execute_sql(
            host=self.application.postgres_host,
            sql=delete_records_sql,
            postgres_pool=self.application.postgres_pool
        )
        delete_predictions_sql = """
            BEGIN;
            DELETE FROM predictions
            WHERE record_id = {record_id}
              AND LOWER(dataset) LIKE '{dataset}';
            COMMIT;
        """.format(
            record_id=record_id,
            dataset=dataset_name
        )
        execute_sql(
            host=self.application.postgres_host,
            sql=delete_predictions_sql,
            postgres_pool=self.application.postgres_pool
        )
        os.remove(label_path)
        os.remove(image_path)

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        yield self.delete_record(json_input=json_input)


class DeleteFlaggedRecord(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def delete_flagged_record(self,json_input):
        dataset_name = json_input['dataset']
        record_id = json_input['record_id']
        self.application.record_reader.write_flag(
            dataset=dataset_name,
            record_id=record_id,
            is_flagged=False
        )
        return {}

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.delete_flagged_record(json_input=json_input)
        self.write(result)

class DeleteFlaggedDataset(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def delete_flagged_dataset(self,json_input):
        dataset_name = json_input['dataset']
        self.application.record_reader.unflag_dataset(
            dataset=dataset_name,
        )
        return {}

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.delete_flagged_dataset(json_input=json_input)
        self.write(result)

class DeleteLaptopDataset(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def delete_dataset(self,json_input):
        dataset_name = json_input['dataset']
        datasets_directory = self.application.scheduler.pi_settings['laptop datasets directory']
        dataset_path = f'{datasets_directory}/{dataset_name}'
        shutil.rmtree(dataset_path)
        execute_sql(
            host=None,
            sql=f"BEGIN; DELETE FROM records WHERE dataset = '{dataset_name}'; COMMIT;",
            postgres_pool=self.application.postgres_pool
        )
        return {}

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.delete_dataset(json_input=json_input)
        self.write(result)


class DeletePiDataset(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(10)

    @tornado.concurrent.run_on_executor
    def delete_dataset(self,json_input):
        dataset_name = json_input['dataset']
        datasets_dir = read_pi_setting(
            host=self.application.postgres_host,
            field_name='pi datasets directory',
            postgres_pool=self.application.postgres_pool
        )
        command = 'sudo rm -rf {datasets_dir}/{dataset_name}'.format(
            datasets_dir=datasets_dir,
            dataset_name=dataset_name
        )
        execute_pi_command(
            postgres_host=self.application.postgres_host,
            command=command,
            pi_credentials=self.application.scheduler.pi_settings
        )
        return {}

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.delete_dataset(json_input=json_input)
        self.write(result)


class TransferDatasetFromPiToLaptop(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(10)

    @tornado.concurrent.run_on_executor
    def transfer_dataset(self,json_input):
        dataset_name = json_input['dataset']

        # Get cached Pi fields from the scheduler
        datasets_dir = self.application.scheduler.pi_settings['pi datasets directory']
        pi_hostname = self.application.scheduler.pi_settings['hostname']
        username = self.application.scheduler.pi_settings['username']
        password = self.application.scheduler.pi_settings['password']
        laptop_datasets_directory = self.application.scheduler.pi_settings['laptop datasets directory']

        from_path = '{datasets_dir}/{dataset_name}'.format(
            datasets_dir=datasets_dir,
            dataset_name=dataset_name
        )
        to_path = '{laptop_datasets_directory}/{dataset_name}'.format(
            laptop_datasets_directory=laptop_datasets_directory,
            dataset_name=dataset_name
        )

        # Add to jobs table for tracking
        add_job(
            postgres_host=self.application.postgres_host,
            session_id=self.application.session_id,
            name='dataset import',
            detail=dataset_name,
            status='pending'
        )

        # Run the SFTP step
        sftp(
            hostname=pi_hostname,
            username=username,
            password=password,
            remotepath=from_path,
            localpath=to_path,
            sftp_type='get'
        )

        # Load the data into Postgres
        for file_path, record_id in self.application.record_reader.ordered_label_files(folder=to_path):
            _, angle, throttle = self.application.record_reader.read_record(label_path=file_path)
            self.application.record_reader.write_new_record_to_db(
                dataset_name=dataset_name,
                record_id=record_id,
                angle=angle,
                throttle=throttle,
                label_file_path=file_path
            )

        # Remove the job from the jobs table, which signifies completion
        delete_job(
            postgres_pool=self.application.postgres_pool,
            job_name='dataset import',
            job_detail=dataset_name,
            session_id=self.application.session_id
        )
        return {}

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.transfer_dataset(json_input=json_input)
        self.write(result)

class ImageCountFromDataset(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def get_image_count(self,json_input):
        dataset_name = json_input['dataset']
        dataset_type = json_input['dataset_type']
        if dataset_type.lower() == 'import':
            # TODO: Change to point to real import datasets
            image_count = self.application.record_reader.get_image_count_from_dataset(
                dataset_name=dataset_name
            )
        elif dataset_type.lower() == 'review':
            sql = f'''
                SELECT
                    COALESCE(count(*),0) AS total
                FROM records
                WHERE LOWER(dataset) = '{dataset_name}'
            '''
            rows = get_sql_rows(sql=sql, postgres_pool=self.application.postgres_pool, host=None)
            return {'image_count': rows[0]['total']}
        elif dataset_type.lower() == 'mistake':
            image_count = self.application.record_reader.get_flagged_record_count(
                dataset_name=dataset_name
            )
        else:
            print('Unknown dataset_type: ' + dataset_type)
        result = {
            'image_count': image_count
        }
        return result

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.get_image_count(json_input=json_input)
        self.write(result)

class DatasetIdFromDataName(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def get_dataset_id_from_name(self,json_input):
        dataset_name = json_input['dataset']
        dataset_id = self.application.record_reader.get_dataset_id_from_dataset_name(
            dataset_name=dataset_name
        )
        result = {
            'dataset_id': dataset_id
        }
        return result

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.get_dataset_id_from_name(json_input=json_input)
        self.write(result)

class DatasetDateFromDataName(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def get_dataset_date(self,json_input):
        dataset_name = json_input['dataset']
        dataset_date = self.application.record_reader.get_dataset_date_from_dataset_name(
            dataset_name=dataset_name
        )
        result = {
            'dataset_date': dataset_date
        }
        return result

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.get_dataset_date(json_input=json_input)
        self.write(result)

class ListReviewDatasets(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def get_review_datasets(self):
        dataset_names = self.application.record_reader.get_dataset_names()
        results = {
            'datasets': dataset_names
        }
        return results

    @tornado.gen.coroutine
    def get(self):
        results = yield self.get_review_datasets()
        self.write(results)


class GetImportRows(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def get_import_datasets(self):
        reocrds = get_pi_dataset_import_stats(
            pi_datasets_dir=self.application.scheduler.pi_settings['pi datasets directory'],
            laptop_dataset_dir=self.application.scheduler.pi_settings['laptop datasets directory'],
            postgres_host=self.application.postgres_host,
            session_id=self.application.session_id,
            service_host=self.application.scheduler.service_host,
            record_tracker_port=self.application.scheduler.get_services()['record-tracker']['port'],
            pi_settings=self.application.scheduler.pi_settings
        )
        return reocrds

    @tornado.gen.coroutine
    def get(self):
        records = yield self.get_import_datasets()
        self.write({'records':records})


class ListReviewDatasetsFileSystem(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def get_review_datasets(self):
        folder_file_paths = self.application.record_reader.folders
        dataset_names = self.application.record_reader.get_dataset_names_filesystem(
            file_paths=folder_file_paths
        )
        results = {
            'datasets': dataset_names
        }
        return results

    @tornado.gen.coroutine
    def get(self):
        results = yield self.get_review_datasets()
        self.write(results)



class ImageAPI(tornado.web.RequestHandler):
    '''
    Serves a MJPEG of the images posted from the vehicle.
    '''

    @tornado.web.asynchronous
    @tornado.gen.coroutine
    def get(self):

        dataset = self.get_argument("dataset")
        record_id = self.get_argument("record-id")
        ioloop = tornado.ioloop.IOLoop.current()
        self.set_header("Content-type", "multipart/x-mixed-replace;boundary=--boundarydonotcross")

        self.served_image_timestamp = time.time()
        my_boundary = "--boundarydonotcross"
        frame = self.application.record_reader.get_image(
            dataset_name=dataset,
            record_id=record_id
        )
        image_scale_args = self.get_arguments(name="scale-factor")
        if len(image_scale_args) > 0:
            scale = int(image_scale_args[0])
            frame = show_resize_effect(
                original_image=frame,
                scale=scale
            )
        crop_percent_args = self.get_arguments(name="crop-percent")
        if len(crop_percent_args) > 0:
            crop_percent = int(crop_percent_args[0])
            frame = pseduo_crop(
                image=frame,
                crop_percent=crop_percent,
                alpha=0.65
            )

        # Can't serve the OpenCV numpy array
        # Tornando: "... only accepts bytes, unicode, and dict objects" (from Tornado error Traceback)
        # The result of cv2.imencode is a tuple like: (True, some_image), but I have no idea what True refers to
        img = cv2.imencode('.jpg', frame)[1].tostring()

        # I have no idea what these lines do, but other people seem to use them, they
        # came with this copied code and I don't want to break something by removing
        self.write(my_boundary)
        self.write("Content-type: image/jpeg\r\n")
        self.write("Content-length: %s\r\n\r\n" % len(img))

        # Serve the image
        self.write(img)

        self.served_image_timestamp = time.time()
        yield tornado.gen.Task(self.flush)


class VideoAPI(tornado.web.RequestHandler):
    '''
    Serves a MJPEG of the images posted from the vehicle.
    '''

    @tornado.gen.coroutine
    def get(self):

        ioloop = tornado.ioloop.IOLoop.current()
        self.set_header("Content-type", "multipart/x-mixed-replace;boundary=--boundarydonotcross")

        self.served_image_timestamp = time.time()
        my_boundary = "--boundarydonotcross"

        while True:

            interval = .1
            if self.served_image_timestamp + interval < time.time():

                """
                Sometimes when the Pi is under heavy load (e.g., when you deploy
                the Tensorlfow model service), the video part has timeouts, which
                leads to images that are None, and this leads to OpenCV empty jpeg
                errors like this:
                    (-10:Unknown error code -10) Raw image encoder error: Empty JPEG image
                The best fix is to make the services perform better under load, but
                it's nice to address the symptom too by asking OpenCV to skip
                encoding of empty images
                """
                if self.application.scheduler.raw_dash_frame is None:
                    continue

                # Can't serve the OpenCV numpy array
                # Tornando: "... only accepts bytes, unicode, and dict objects" (from Tornado error Traceback)
                # The result of cv2.imencode is a tuple like: (True, some_image), but I have no idea what True refers to
                img = cv2.imencode('.jpg', self.application.scheduler.raw_dash_frame)[1].tostring()

                # I have no idea what these lines do, but other people seem to use them, they
                # came with this copied code and I don't want to break something by removing
                self.write(my_boundary)
                self.write("Content-type: image/jpeg\r\n")
                self.write("Content-length: %s\r\n\r\n" % len(img))

                # Serve the image
                self.write(img)

                self.served_image_timestamp = time.time()
                yield tornado.gen.Task(self.flush)
            else:
                yield tornado.gen.Task(ioloop.add_timeout, ioloop.time() + interval)


class PS3ControllerSixAxisStart(tornado.web.RequestHandler):

    """
    Start the SixAxis module so that commands from the controller
    can be relayed to the car
    """

    executor = ThreadPoolExecutor(3)

    @tornado.concurrent.run_on_executor
    def start_sixaxis_loop(self, json_input):
        host = json_input['host']
        port = json_input['port']
        try:
            seconds = 0.5
            endpoint = 'http://{host}:{port}/start-sixaxis-loop'.format(
                host=host,
                port=port
            )
            response = requests.post(
                endpoint,
                timeout=seconds
            )
            result = json.loads(response.text)
            return result
        except:
            return {'is_healthy': False}

    @tornado.gen.coroutine
    def post(self):
        result = {}
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.start_sixaxis_loop(json_input=json_input)
        self.write(result)


# TODO: Make this just a DB lookup and perform health check in the scheduler
class IsPS3ControllerConnected(tornado.web.RequestHandler):

    """
    This says if js0 is available at /dev/input. If true, it
    means that the controller is either connected using the
    cable or Bluetooth. It should not be confused with the
    controller health check, which checks if the SixAxis
    module is able to connect. I'm keeping them separate to
    make it easier to find the root cause of the problem if
    a problem arises
    """

    executor = ThreadPoolExecutor(3)

    @tornado.concurrent.run_on_executor
    def is_connected(self):
        try:
            seconds = 0.5
            endpoint = 'http://{host}:{port}/is-connected'.format(
                host=self.application.scheduler.service_host,
                port=8094  # TODO: Get from the scheduler, which gets from DB
            )
            response = requests.post(
                endpoint,
                timeout=seconds
            )
            result = json.loads(response.text)
            return result
        except:
            return {'is_connected': False}

    @tornado.gen.coroutine
    def post(self):
        result = yield self.is_connected()
        self.write(result)


class InitiaizePS3Setup(tornado.web.RequestHandler):

    """
    This class removes all PS3 controllers from the list of devices
    that you see in the bluetoothctl console when you type `devices`

    The instructions I've read online assume you're only using one
    PS3 device. It assumes that when you type the "devices" command
    you'll know which MAC address to copy because you'll only see
    one PS3 controller. If you have multiple registered PS3
    controllers, then you will have no way to tell which is which.
    The physical PS3 does not have any label about its MAC address
    so you couldn't figure it out even if you wanted to. So, what
    should you do if you need multiple controllers, for example if
    you're at a live event, and the battery of your first controller
    dies and you need the second? Assume that you will need to go
    through the registration process all over again with the second
    controller, which means wiping all registered controllers from
    the list of registered devices. That is what this class does.
    """

    executor = ThreadPoolExecutor(10)

    @tornado.concurrent.run_on_executor
    def run(self, json_input):
        host = json_input['host']
        port = json_input['port']
        try:
            seconds = 3.0
            endpoint = 'http://{host}:{port}/is-ps3-connected'.format(
                host=host,
                port=port
            )
            _ = requests.post(
                endpoint,
                timeout=seconds,
                data = json.dumps(json_input)
            )
        except:
            return {'is_success': False}
        return {'is_success': True}

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.run(json_input=json_input)
        self.write(result)


class PS3SudoSixPair(tornado.web.RequestHandler):

    """
    This runs the first PS3 step, calling `sudo sixpair`. It
    has the annoying side effect of making it appear as though
    the user has unplugged the controller, but this annoying
    behavior is expected, according to the official docus:
    https://pythonhosted.org/triangula/sixaxis.html. Anyways,
    The user will need to reconnect after this step is run
    """

    executor = ThreadPoolExecutor(3)

    @tornado.concurrent.run_on_executor
    def run_sudo_sixpair(self, json_input):
        host = json_input['host']
        port = json_input['port']
        try:
            seconds = 1.0
            endpoint = 'http://{host}:{port}/sudo-sixpair'.format(
                host=host,
                port=port
            )
            _ = requests.post(
                endpoint,
                timeout=seconds
            )
            return {'is_success':True}
        except:
            return {'is_success':False}

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.run_sudo_sixpair(json_input=json_input)
        self.write(result)


class RunPS3Setup(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(10)

    @tornado.concurrent.run_on_executor
    def run_setup(self, json_input):
        host = json_input['host']
        port = json_input['port']
        try:
            seconds = 5
            endpoint = 'http://{host}:{port}/run-setup-commands'.format(
                host=host,
                port=port
            )
            response = requests.post(
                endpoint,
                timeout=seconds
            )
            _ = json.loads(response.text)
            return {'is_success':True}
        except:
            return {'is_success': False}

    @tornado.gen.coroutine
    def post(self):
        result = {}
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.run_setup(json_input=json_input)
        self.write(result)


class StopService(tornado.web.RequestHandler):
    """
    Calling this class will signal to the scheduler that the service
    should be stopped. This class does not directly call the stop
    service, however, it only stops it indirectly
    """
    async def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        service = json_input['service']

        self.write({})

class StartCarService(tornado.web.RequestHandler):
    """
    Calling this class will signal to the scheduler that the service
    should be started. This class does not directly call the start
    service, however, it only start it indirectly
    """
    async def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        service = json_input['json_input']
        """
        I record when I start and stop so that I can check if I recently start
        or stopped the service. Hopefully this will make the services more
        stable, and allows me to decouple the healthcheck interval from the
        service restart interval, since some services take awhile to start up
        """
        service_event_sql = '''
            BEGIN;
            INSERT INTO service_event(
                event_time,
                service,
                event,
                host
            )
            VALUES (
                NOW(),
                '{service}',
                'start',
                '{host}'
            );
            COMMIT;
        '''
        await execute_sql_aio(host=self.application.postgres_host, sql=service_event_sql.format(
                service=service,
                host=self.application.scheduler.service_host,
                postgres_pool=self.application.postgres_pool
            ),
            aiopg_pool=self.application.scheduler.aiopg_pool)
        self.write({})


class PiHealthCheck(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def health_check(self):
        asyncio.set_event_loop(asyncio.new_event_loop())
        return {
            'is_able_to_connect':is_pi_healthy(
                postgres_host=self.application.postgres_host,
                command='ls -ltr',
                pi_credentials=self.application.scheduler.pi_settings
            )
        }

    @tornado.gen.coroutine
    def post(self):
        result = yield self.health_check()
        self.write(result)


class PS3ControllerHealth(tornado.web.RequestHandler):

    """
    This should not be confused with the health of the
    PS3 controller service. This checks if the SixAxis
    (custom PS3 module) is able to connect to the
    controller. The PS3 controller service might be up
    and healthy, but it might not be connected to the
    controller. This will always be true the before you
    have paired the controller with the service
    """

    # Need lots of threads because there are many services
    executor = ThreadPoolExecutor(3)

    @tornado.concurrent.run_on_executor
    def health_check(self):
        # TODO: Remove this hardcoded port
        port = 8094

        try:
            seconds = 3.0
            endpoint = 'http://{host}:{port}/ps3-health'.format(
                host=self.application.scheduler.service_host,
                port=port
            )
            response = requests.get(
                endpoint,
                timeout=seconds
            )
            result = json.loads(response.text)
            return result
        except:
            return {'is_healthy': False}

    @tornado.gen.coroutine
    def post(self):
        result = yield self.health_check()
        self.write(result)


class PiServiceStatus(tornado.web.RequestHandler):
    """
    Used to decouple the service startup frequency from the health
    check frequency, since some services can take more time to
    start up than I would like to wait for a health check update if
    it were already running. Without the de-coupling, services that
    are slow to start get killed prematurely. I use an independent,
    scheduled health check loop to call health check APIs. All this
    API does is check a bunch of asynchronously gathered metrics to
    make a conclusion about the latest state of a given service

    Returns one of several statuses:
        - starting-up
        - healthy
        - unhealthy
        - shutting-down
        - off
        - invincible-zombie
    """

    async def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        service = json_input['service']
        status = await get_service_status(
            postgres_host=self.application.postgres_host,
            service_host=self.application.scheduler.service_host,
            service=service,
            aiopg_pool=self.application.scheduler.aiopg_pool
        )
        self.write({'status':status})

class ResumeTraining(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def resume_training(self, json_input):
        model_id = json_input['model_id']
        datasets_path = read_pi_setting(
            host=self.application.postgres_host,
            field_name='laptop datasets directory',
            postgres_pool=self.application.postgres_pool
        )
        model_base_directory = read_pi_setting(
            host=self.application.postgres_host,
            field_name='models_location_laptop',
            postgres_pool=self.application.postgres_pool
        )

        """
        Used to track whether a model is training or not. This is
        much faster than calling the health check (used for the UI)
        because it can take awhile to load the model and get
        everything started up whereas it's quick to check if a model
        /should/ be training. This makes the train button much
        more responsive and gives much better user feedback
        """
        add_job(
            postgres_host=None,
            session_id=self.application.session_id,
            name='machine learning',
            detail='training',
            status='started',
            postgres_pool=self.application.postgres_pool
        )

        resume_training(
            postgres_host=self.application.postgres_host,
            model_id=model_id,
            host_data_path=datasets_path,
            model_base_directory=model_base_directory,
            port=self.application.scheduler.get_services()['model-training']['port']
        )
        return {}

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.resume_training(json_input=json_input)
        self.write(result)


class StopTraining(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def stop_training(self):
        delete_job(
            session_id=self.application.session_id,
            job_name='machine learning',
            job_detail='training',
            postgres_pool=self.application.postgres_pool
        )
        stop_training()
        return {}

    @tornado.gen.coroutine
    def post(self):
        result = yield self.stop_training()
        self.write(result)


class TrainNewModel(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def train_new_model(self, json_input):
        data_path = read_pi_setting(
            host=self.application.postgres_host,
            field_name='laptop datasets directory',
            postgres_pool=self.application.postgres_pool
        )
        model_base_directory = read_pi_setting(
            host=self.application.postgres_host,
            field_name='models_location_laptop',
            postgres_pool=self.application.postgres_pool
        )

        """
        Used to track whether a model is training or not. This is
        much faster than calling the health check (used for the UI)
        because it can take awhile to load the model and get
        everything started up whereas it's quick to check if a model
        /should/ be training. This makes the train button much
        more responsive and gives much better user feedback
        """
        add_job(
            postgres_host=None,
            session_id=self.application.session_id,
            name='machine learning',
            detail='training',
            status='started',
            postgres_pool=self.application.postgres_pool
        )

        train_new_model(
            postgres_host=self.application.postgres_host,
            model_base_directory=model_base_directory,
            data_path=data_path,
            epochs=100,
            image_scale=json_input['scale'],
            crop_percent=json_input['crop_percent'],
            port=self.application.scheduler.get_services()['model-training']['port']
        )
        return {}

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.train_new_model(json_input=json_input)
        self.write(result)


class IsTrainingJobSubmitted(tornado.web.RequestHandler):

    async def get(self):

        session_id = self.application.session_id
        sql_query = f"""
        SELECT
            status
        FROM jobs
        WHERE
            LOWER(name) = 'machine learning'
            AND LOWER(detail) = 'training'
            AND LOWER(session_id) = '{session_id}'
        """
        rows = await get_sql_rows_aio(
            host=None,
            sql=sql_query,
            aiopg_pool=self.application.scheduler.aiopg_pool
        )
        if len(rows) > 0:
            self.write({'is_alive': True})
        else:
            self.write({'is_alive': False})


class GetTrainingMetadata(tornado.web.RequestHandler):
    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def health_check(self):
        seconds = 0.5
        try:
            # TODO: Remove hardcoded port
            request = requests.post(
                'http://localhost:{port}/training-state'.format(
                    port=self.application.scheduler.get_services()['model-training']['port']
                ),
                timeout=seconds
            )
            response = json.loads(request.text)
            response['is_alive'] = True
            return response
        except:
            return {'is_alive': False}

    @tornado.gen.coroutine
    def get(self):
        result = yield self.health_check()
        self.write(result)



class DoesModelAlreadyExist(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def does_model_exist(self):
        exists = os.path.exists(self.application.model_path)
        result = {'exists': exists}
        return result

    @tornado.gen.coroutine
    def post(self):
        result = yield self.does_model_exist()
        self.write(result)


class BatchPredict(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def batch_predict(self,json_input):
        dataset_name = json_input['dataset']
        process = batch_predict(
            dataset=dataset_name,
            # TODO: Remove this hardcoded port
            predictions_port=self.application.scheduler.get_services()['angle-model-laptop']['port'],
            datasets_port=self.application.port
        )
        result = {}
        return result

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.batch_predict(json_input=json_input)
        self.write(result)

class NewEpochs(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def get_epochs(self,json_inputs):
        model_id = json_inputs['model_id']
        sql_query = '''
            WITH recent_epochs AS (
                SELECT
                  epochs.epoch,
                  epochs.train,
                  epochs.validation
                FROM epochs
                WHERE epochs.model_id = {model_id}
                ORDER BY epochs.epoch DESC
                LIMIT 10
            )

            SELECT
              *
            FROM recent_epochs
            ORDER BY epoch ASC
        '''.format(
            model_id=model_id
        )
        epochs = get_sql_rows(
            host=self.application.postgres_host,
            sql=sql_query,
            postgres_pool=self.application.postgres_pool
        )
        result = {
            'epochs':epochs
        }
        return result

    @tornado.gen.coroutine
    def post(self):
        json_inputs = tornado.escape.json_decode(self.request.body)
        result = yield self.get_epochs(json_inputs=json_inputs)
        self.write(result)


class HighestModelEpoch(tornado.web.RequestHandler):

    """
    Used in the machine learning page's models table to show which
    models might have been trained by mistake. Models that were
    trained by mistake won't have many epochs (e.g., 0)
    """

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def get_highest_model_epoch(self,json_inputs):
        model_id = json_inputs['model_id']
        sql_query = '''
            SELECT
                COALESCE(MAX(epoch),0) AS max_epoch
            FROM epochs
            WHERE model_id = {model_id}
        '''.format(
            model_id=model_id
        )
        max_epoch = get_sql_rows(
            host=self.application.postgres_host,
            sql=sql_query,
            postgres_pool=self.application.postgres_pool
        )[0]['max_epoch']
        result = {
            'max_epoch':max_epoch
        }
        return result

    @tornado.gen.coroutine
    def post(self):
        json_inputs = tornado.escape.json_decode(self.request.body)
        result = yield self.get_highest_model_epoch(json_inputs=json_inputs)
        self.write(result)

class RefreshRecordReader(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def refresh(self):
        self.application.record_reader.refresh_folders()
        return {}

    @tornado.gen.coroutine
    def post(self):
        result = yield self.refresh()
        self.write(result)


class DatasetPredictionUpdateStatuses(tornado.web.RequestHandler):

    """
    Gets the "analyze", "error", and "critical" fields for each row in
    the datasets review table. The predecessor to this endpoint got
    called for each dataset separately, and this led to awful performance
    as the number of datasets grew

    Shows the "analyze metrics" for the device that you selected in the
    machine learning page that should steer the car (could be laptop
    "remote_model" or pi "local_model"). The distinction by device type is
    important because the model version on the Pi could be different from
    the model version on the laptop, since the services are deployed
    separately
    """

    executor = ThreadPoolExecutor(5)

    @tornado.concurrent.run_on_executor
    def get_data(self):
        sql_query = '''
        WITH radio_model_device_type AS (
            SELECT
              detail AS device_type
            FROM toggles
            WHERE LOWER(web_page) = 'machine learning'
              AND LOWER(name) = 'driver-device-type'
              AND is_on = TRUE
            ORDER BY event_ts DESC
        ),

        device_deployments AS (
          SELECT
            model_id,
            epoch_id,
            ROW_NUMBER() OVER(PARTITION BY device ORDER BY event_ts DESC) AS latest_rank
          FROM deployments
          JOIN radio_model_device_type
              ON LOWER(deployments.device) = LOWER(radio_model_device_type.device_type)
        ),

        latest_deployment AS (
          SELECT
              model_id,
              epoch_id
            FROM device_deployments
            WHERE latest_rank = 1
        ),

        metrics AS (
          SELECT
            records.dataset,
            AVG(CASE
              WHEN predictions.epoch IS NOT NULL
                THEN 100.0
              ELSE 0.0 END) = 100 AS is_up_to_date,
            AVG(CASE
              WHEN predictions.angle IS NOT NULL
                THEN 100.0
              ELSE 0.0 END) AS completion_percent,
            SUM(CASE WHEN ABS(records.angle - predictions.angle) >= 0.8
                THEN 1 ELSE 0 END) AS critical_count,
            AVG(CASE WHEN ABS(records.angle - predictions.angle) >= 0.8
              THEN 100.0 ELSE 0.0 END)::FLOAT AS critical_percent,
            AVG(ABS(records.angle - predictions.angle)) AS avg_abs_error,
            COUNT(*) AS prediction_count
        FROM records
        LEFT JOIN latest_deployment AS deploy
          ON TRUE
        LEFT JOIN predictions
          ON records.dataset = predictions.dataset
            AND records.record_id = predictions.record_id
            AND deploy.model_id = predictions.model_id
            AND deploy.epoch_id = predictions.epoch
        GROUP BY records.dataset
        ORDER BY dataset
        ),

        prediction_syncs AS (
          SELECT
            dataset,
            COUNT(*) > 0 AS answer
          FROM live_prediction_sync
          GROUP BY dataset
        )

        SELECT
          metrics.dataset,
          metrics.is_up_to_date,
          metrics.completion_percent::FLOAT AS completion_percent,
          metrics.critical_count::FLOAT AS critical_count,
          metrics.critical_percent::FLOAT AS critical_percent,
          metrics.avg_abs_error::FLOAT AS avg_abs_error,
          metrics.prediction_count::FLOAT AS prediction_count,
          COALESCE(prediction_syncs.answer,FALSE) AS is_syncing
        FROM metrics
        LEFT JOIN prediction_syncs
          ON metrics.dataset = prediction_syncs.dataset
        '''
        rows = get_sql_rows(
            host=self.application.postgres_host,
            sql=sql_query,
            postgres_pool=self.application.postgres_pool
        )
        result = {
            'rows': rows
        }
        return result

    @tornado.gen.coroutine
    def get(self):
        result = yield self.get_data()
        self.write(result)


class GetNextDatasetName(tornado.web.RequestHandler):

    """
    Returns what the next dataset would be, if it were created. Not to
    be confused with actually creating a new dataset, however. I want
    to separate the lookup from the creation because I need to pass a
    dataset name to the UI when the user first visits the dashboard,
    and if the user doesn't start recording data but frequently moves
    across pages, I don't want to end up with a bunch of empty dataset
    folders on the Pi
    """

    executor = ThreadPoolExecutor(3)

    @tornado.concurrent.run_on_executor
    def get_next_dataset_name(self, json_input):
        host = json_input['host']
        port = 8093  # TODO: Look up this service's port in a DB
        try:
            seconds = 1.0
            endpoint = 'http://{host}:{port}/get-next-dataset-name'.format(
                host=host,
                port=port
            )
            response = requests.get(
                endpoint,
                timeout=seconds
            )
            dataset = json.loads(response.text)  # Return from the server as-is
            return dataset
        except:
            return dataset

    @tornado.gen.coroutine
    def post(self):
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.get_next_dataset_name(json_input=json_input)
        self.write(result)

class GetPiDatasetName(tornado.web.RequestHandler):

    """
    Returns the name of the dataset that you would end up
    writing to if you tried to write a record
    """

    async def get(self):
        timeout_seconds = 3.0
        timeout = ClientTimeout(total=timeout_seconds)
        endpoint = 'http://{host}:{port}/get-current-dataset-name'.format(
            host=self.application.scheduler.service_host,
            port=8093  # TODO: Look up this service's port in a DB
        )
        async with ClientSession(timeout=timeout) as session:
            async with session.get(endpoint) as response:
                result = await response.json()
                dataset = result['dataset']
                dataset_id = self.application.record_reader.get_dataset_id_from_dataset_name(
                    dataset_name=dataset
                )
                self.write({
                    'dataset': dataset,
                    'dataset_id': dataset_id
                })

class CreateNewDataset(tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(10)

    @tornado.concurrent.run_on_executor
    def run_setup(self, json_input):
        host = json_input['host']
        port = json_input['port']
        try:
            seconds = 1.0
            endpoint = 'http://{host}:{port}/create-new-dataset'.format(
                host=host,
                port=port
            )
            response = requests.get(
                endpoint,
                timeout=seconds
            )
            dataset = json.loads(response.text)  # Return from the server as-is
            return dataset
        except:
            return {'dataset': -1}

    @tornado.gen.coroutine
    def post(self):
        result = {}
        json_input = tornado.escape.json_decode(self.request.body)
        result = yield self.run_setup(json_input=json_input)
        self.write(result)


def make_app():
    this_dir = os.path.dirname(os.path.realpath(__file__))
    assets_absolute_path = os.path.join(this_dir, 'dist', 'assets')
    html_absolute_path = os.path.join(this_dir, 'dist')
    handlers = [
        (r"/", tornado.web.RedirectHandler, dict(url="/raspberry-pi.html")),
        (r"/home", Home),
        (r"/ai-angle", AIAngleAPI),
        (r"/user-labels", UserLabelsAPI),
        (r"/image", ImageAPI),
        (r"/video", VideoAPI),
        (r"/new-dataset-name", NewDatasetName),
        (r"/dataset-record-ids",DatasetRecordIdsAPI),
        (r"/dataset-record-ids-filesystem", DatasetRecordIdsAPIFileSystem),
        (r"/deployment-health", DeploymentHealth),
        (r"/delete-model", DeleteModel),
        (r"/delete",DeleteRecord),
        (r"/delete-laptop-dataset", DeleteLaptopDataset),
        (r"/delete-pi-dataset", DeletePiDataset),
        (r"/transfer-dataset", TransferDatasetFromPiToLaptop),
        (r"/save-reocord-to-db", SaveRecordToDB),
        (r"/delete-flagged-record", DeleteFlaggedRecord),
        (r"/delete-flagged-dataset", DeleteFlaggedDataset),
        (r"/add-flagged-record", Keep),
        (r"/list-models", ListModels),
        (r"/list-import-datasets", GetImportRows),
        (r"/list-review-datasets", ListReviewDatasets),
        (r"/list-datasets-filesystem", ListReviewDatasetsFileSystem),
        (r"/image-count-from-dataset", ImageCountFromDataset),
        (r"/is-record-already-flagged", IsRecordAlreadyFlagged),
        (r"/dataset-id-from-dataset-name", DatasetIdFromDataName),
        (r"/dataset-date-from-dataset-name", DatasetDateFromDataName),
        (r"/(.*.html)", tornado.web.StaticFileHandler, {"path": html_absolute_path}),
        (r"/assets/(.*)", tornado.web.StaticFileHandler, {"path": assets_absolute_path}),
        (r"/resume-training", ResumeTraining),
        (r"/stop-training", StopTraining),
        (r"/train-new-model", TrainNewModel),
        (r"/list-model-deployments", ListModelDeployments),
        (r"/update-deployments-table", UpdateDeploymentsTable),
        (r"/deploy-model", DeployModel),
        (r"/is-training-job-submitted", IsTrainingJobSubmitted),
        (r"/get-training-metadata", GetTrainingMetadata),
        (r"/does-model-already-exist", DoesModelAlreadyExist),
        (r"/batch-predict", BatchPredict),
        (r"/get-dataset-prediction-update-statuses", DatasetPredictionUpdateStatuses),
        (r"/get-new-epochs", NewEpochs),
        (r"/write-toggle", WriteToggle),
        (r"/read-toggle", ReadToggle),
        (r"/write-slider", WriteSlider),
        (r"/read-slider", ReadSlider),
        (r"/write-pi-field", WritePiField),
        (r"/read-pi-field", ReadPiField),
        (r"/refresh-record-reader", RefreshRecordReader),
        (r"/raspberry-pi-healthcheck", PiHealthCheck),
        (r"/highest-model-epoch", HighestModelEpoch),
        (r"/start-car-service", StartCarService),
        (r"/vehicle-memory", Memory),
        (r"/pi-service-status", PiServiceStatus),
        (r"/stop-service", StopService),
        (r"/initialize-ps3-setup", InitiaizePS3Setup),
        (r"/run-ps3-setup-commands", RunPS3Setup),
        (r"/ps3-controller-health", PS3ControllerHealth),
        (r"/start-sixaxis-loop", PS3ControllerSixAxisStart),
        (r"/is-ps3-connected", IsPS3ControllerConnected),
        (r"/sudo-sixpair", PS3SudoSixPair),
        (r"/laptop-model-api-health", LaptopModelAPIHealth),
        (r"/create-new-dataset", CreateNewDataset),
        (r"/get-next-dataset-name", GetNextDatasetName),
        (r"/get-pi-dataset-name", GetPiDatasetName)
    ]
    return tornado.web.Application(handlers)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--port",
        required=False,
        help="Server port to use",
        default=8883)
    ap.add_argument(
        "--new_data_path",
        required=False,
        help="Where to store emphasized images",
        default='/Users/ryanzotti/Documents/Data/Self-Driving-Car/printer-paper/emphasis-data/dataset')
    ap.add_argument(
        "--angle_only",
        required=False,
        help="Use angle only model (Y/N)?",
        default='y')
    args = vars(ap.parse_args())
    if 'y' in args['angle_only'].lower():
        args['angle_only'] = True
    else:
        args['angle_only'] = False
    port = args['port']
    app = make_app()
    app.port = port
    app.angle = 0.0
    app.throttle = 0.0
    app.mode = 'user'
    app.recording = False
    app.brake = True
    app.max_throttle = 1.0
    app.new_data_path = args['new_data_path']

    # TODO: Remove hard coded ref
    """
    The Postgres host is what editor.py tells record_reader.py
    the host to reach Postgres. If you run editor.py from PyCharm
    in your laptop this will be localhost. Eventually once the
    code has stabilized and editor.py is run inside of a container
    you'll need to use the named container instead
    """
    postgres_host = 'localhost'
    app.postgres_host = postgres_host

    app.postgres_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=10,
        maxconn=10,
        user="postgres",
        password="",
        host=app.postgres_host,
        port="5432",
        database="autonomous_vehicle"
    )

    """
    The API only uses paths from the DB, so I pass a meaningless
    value here. RecordReader might still need the path for model
    training (or something else), so I haven't removed it from the
    class entirely
    """
    record_reader_base_directory = '/'

    app.record_reader = RecordReader(
        base_directory=record_reader_base_directory,
        postgres_host=app.postgres_host,
        overfit=False
    )

    """
    I have a SQL table called "jobs" that I use to track
    the status of various background processes, such as
    SFTP file transfers when import a dataset to my
    laptop from the Pi. I use a session_id that is unique
    to each editor.py invocation to distinguish current
    jobs vs old jobs. When I start up the editor.py server
    for the first time I perform clean up the jobs table
    by removing anything that is not from the current run
    """
    app.session_id = uuid4()
    delete_stale_jobs(
        postgres_host=app.postgres_host,
        session_id=app.session_id
    )

    app.angle_only = args['angle_only']
    app.scheduler = Scheduler(
        postgres_host=postgres_host,
        session_id=app.session_id
    )
    app.listen(port)

    # Make sure to kill any old zombie training jobs
    await stop_training_aio()

    # Used to run a bunch of async tasks
    await app.scheduler.start()

if __name__ == "__main__":
    asyncio.run(main())
