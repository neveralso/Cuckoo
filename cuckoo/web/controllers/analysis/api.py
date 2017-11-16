# Copyright (C) 2016-2017 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import calendar
import datetime
import dateutil.relativedelta
import io
import os
import pymongo
import sqlalchemy
import tarfile
import zipfile
import copy
import re

from django.http import JsonResponse, HttpResponse
from django.core.cache import caches
from wsgiref.util import FileWrapper

from cuckoo.common.exceptions import CuckooFeedbackError
from cuckoo.common.files import Folders
from cuckoo.common.mongo import mongo
from cuckoo.common.utils import list_of_strings, list_of_ints
from cuckoo.core.database import (
    Database, Task, TASK_RUNNING, TASK_REPORTED, TASK_COMPLETED
)
from cuckoo.core.feedback import CuckooFeedback
from cuckoo.misc import cwd
from cuckoo.web.utils import (
    api_post, api_get, file_response, json_error_response,
    json_fatal_response, normalize_task
)

db = Database()


class AnalysisApi(object):
    @api_post
    def tasks_list(request, body):
        completed_after = body.get("completed_after")
        if completed_after:
            completed_after = datetime.datetime.fromtimestamp(
                int(completed_after)
            )

        data = {
            "tasks": []
        }

        limit = body.get("limit")
        offset = body.get("offset")
        owner = body.get("owner")
        status = body.get("status")

        for row in db.list_tasks(limit=limit, details=True, offset=offset,
                                 completed_after=completed_after, owner=owner,
                                 status=status, order_by=Task.completed_on.asc()):
            task = row.to_dict()

            # Sanitize the target in case it contains non-ASCII characters as we
            # can't pass along an encoding to flask's jsonify().
            task["target"] = task["target"].decode("latin-1")

            task["guest"] = {}
            if row.guest:
                task["guest"] = row.guest.to_dict()

            task["errors"] = []
            for error in row.errors:
                task["errors"].append(error.message)

            task["sample"] = {}
            if row.sample_id:
                sample = db.view_sample(row.sample_id)
                task["sample"] = sample.to_dict()

            data["tasks"].append(task)

        return JsonResponse({"status": True, "data": data}, safe=False)

    @api_get
    def task_info(request, task_id):
        try:
            return JsonResponse({
                "status": True,
                "data": {
                    # TODO Test correctness of the following.
                    "task": normalize_task(db.view_task(task_id)),
                },
            }, safe=False)
        except Exception as e:
            return json_error_response(str(e))

    @api_post
    def tasks_info(request, body):
        task_ids = body.get("task_ids", [])
        if not list_of_ints(task_ids):
            return json_error_response("Invalid task IDs given!")

        data = {}
        for task in db.view_tasks(task_ids):
            data[task.id] = normalize_task(task.to_dict())
        return JsonResponse({"status": True, "data": data}, safe=False)

    @api_get
    def task_delete(request, task_id):
        """
        Deletes a task
        :param body: required: task_id
        :return:
        """
        task = db.view_task(task_id)
        if task:
            if task.status == TASK_RUNNING:
                return json_fatal_response("The task is currently being "
                                           "processed, cannot delete")

            if db.delete_task(task_id):
                Folders.delete(os.path.join(cwd(), "storage",
                                            "analyses", "%d" % task_id))
            else:
                return json_fatal_response("An error occurred while trying to "
                                           "delete the task")
        else:
            return json_error_response("Task not found")

        return JsonResponse({"status": True})

    @api_get
    def tasks_reschedule(request, task_id, priority=None):
        """
        Reschedules a task
        :param body: required: task_id, priority
        :return: new task_id
        """
        if not db.view_task(task_id):
            return json_error_response("There is no analysis with the specified ID")

        new_task_id = db.reschedule(task_id, priority)
        if new_task_id:
            return JsonResponse({"status": True, "task_id": new_task_id}, safe=False)
        else:
            return json_fatal_response("An error occurred while trying to "
                                       "reschedule the task")

    @api_get
    def task_rereport(request, body):
        task_id = body.get("task_id")
        if not task_id:
            return json_error_response("Task not set")

        task = db.view_task(task_id)
        if task:
            if task.status == TASK_REPORTED:
                db.set_status(task_id, TASK_COMPLETED)
                return JsonResponse({"status": True})

            return JsonResponse({"status": False})

        return json_error_response("Task not found")

    @api_get
    def task_screenshots(request, task_id, screenshot=None):
        folder_path = os.path.join(cwd(), "storage", "analyses", str(task_id), "shots")

        if os.path.exists(folder_path):
            if screenshot:
                screenshot_name = "{0}.jpg".format(screenshot)
                screenshot_path = os.path.join(folder_path, screenshot_name)
                if os.path.exists(screenshot_path):
                    response = HttpResponse(FileWrapper(open(screenshot_path, "rb")),
                                            content_type='image/jpeg')
                    return response
                else:
                    return json_error_response("Screenshot not found")
            else:
                zip_data = io.BytesIO()
                zip_file = zipfile.ZipFile(zip_data, "w", zipfile.ZIP_STORED)
                for shot_name in os.listdir(folder_path):
                    zip_file.write(os.path.join(folder_path, shot_name), shot_name)
                zip_file.close()

                zip_data.seek(0)

                response = file_response(data=zip_data,
                                         filename="analysis_screenshots_%s.tar" % str(task_id),
                                         content_type="application/zip")
                return response

        return json_error_response("Task not found")

    @api_get
    def task_report(request, task_id, report_format="json"):
        # @TO-DO: test /api/task/report/<task_id>/all/?tarmode=bz2
        # duplicate filenames?
        task_id = int(task_id)
        tarmode = request.REQUEST.get("tarmode", "bz2")

        formats = {
            "json": "report.json",
            "html": "report.html",
        }

        bz_formats = {
            "all": {"type": "-", "files": ["memory.dmp"]},
            "dropped": {"type": "+", "files": ["files"]},
            "package_files": {"type": "+", "files": ["package_files"]},
        }

        tar_formats = {
            "bz2": "w:bz2",
            "gz": "w:gz",
            "tar": "w",
        }

        if report_format.lower() in formats:
            report_path = os.path.join(cwd(), "storage", "analyses",
                                       str(task_id), "reports",
                                       formats[report_format.lower()])
        elif report_format.lower() in bz_formats:
            bzf = bz_formats[report_format.lower()]
            srcdir = os.path.join(cwd(), "storage",
                                  "analyses", str(task_id))

            s = io.BytesIO()

            # By default go for bz2 encoded tar files (for legacy reasons).
            if tarmode not in tar_formats:
                tarmode = tar_formats["bz2"]
            else:
                tarmode = tar_formats[tarmode]

            tar = tarfile.open(fileobj=s, mode=tarmode, dereference=True)
            for filedir in os.listdir(srcdir):
                filepath = os.path.join(srcdir, filedir)
                if not os.path.exists(filepath):
                    continue

                if bzf["type"] == "-" and filedir not in bzf["files"]:
                    tar.add(filepath, arcname=filedir)
                if bzf["type"] == "+" and filedir in bzf["files"]:
                    tar.add(filepath, arcname=filedir)

            tar.close()
            s.seek(0)

            response = file_response(data=s, filename="analysis_report_%s.tar" % str(task_id),
                                     content_type="application/x-tar; charset=UTF-8")
            return response
        else:
            return json_fatal_response("Invalid report format")

        if os.path.exists(report_path):
            if report_format == "json":
                response = file_response(data=open(report_path, "rb"),
                                         filename="analysis_report_%s.json" % str(task_id),
                                         content_type="application/json; charset=UTF-8")
                return response
            else:
                return open(report_path, "rb").read()
        else:
            return json_error_response("Report not found")

    @api_post
    def tasks_recent(request, body):
        limit = body.get("limit", 100)
        offset = body.get("offset", 0)

        # Various filters.
        cats = body.get("cats")
        packs = body.get("packs")
        score_range = body.get("score")

        filters = {}

        if cats:
            if not list_of_strings(cats):
                return json_error_response("invalid categories")
            filters["info.category"] = {"$in": cats}

        if packs:
            if not list_of_strings(packs):
                return json_error_response("invalid packages")
            filters["info.package"] = {"$in": packs}

        if score_range and isinstance(score_range, basestring):
            if score_range.count("-") != 1:
                return json_error_response("faulty score")

            score_min, score_max = score_range.split("-")
            if not score_min.isdigit() or not score_max.isdigit():
                return json_error_response("faulty score")

            score_min = int(score_min)
            score_max = int(score_max)

            if score_min < 0 or score_min > 11:
                return json_error_response("faulty score")

            if score_max < 0 or score_max > 11:
                return json_error_response("faulty score")

            # Because scores can be higher than 10.
            # TODO Once we start capping the score, limit this naturally.
            if score_max == 10:
                score_max = 999

            filters["info.score"] = {
                "$gte": score_min,
                "$lt": score_max,
            }

        if not isinstance(offset, (int, long)):
            return json_error_response("invalid offset")

        if not isinstance(limit, (int, long)):
            return json_error_response("invalid limit")

        # TODO Use a mongodb abstraction class once there is one.
        # cursor = mongo.db.analysis.find(
        #     filters, ["info", "target"],
        #     sort=[("info.id", pymongo.DESCENDING)]
        # ).limit(limit).skip(offset)

        pipeline = [
            {
                "$project":
                    {
                        "info": 1,
                        "target": 1
                    }
            },
            {
                "$match":
                    filters
            },
            {
                "$group": {
                    "_id": {"id": "$info.id"},
                    "info": {"$last": "$info"},
                    "target": {"$last": "$target"}
                }
            },
            # {"$sort": {"_id.id": 1}},
            {"$limit": limit},


        ]
        cursor = mongo.db.analysis.aggregate(pipeline)

        tasks = {}
        for row in cursor:
            info = row.get("info", {})
            if not info:
                continue

            category = info.get("category")
            if category == "file":
                f = row.get("target", {}).get("file", {})
                if f.get("name"):
                    target = os.path.basename(f["name"])
                else:
                    target = None
                target_type = f.get("type", "Not detected")
                md5 = f.get("md5") or "-"
            elif category == "url":
                target = row["target"]["url"]
                target_type = "URL"
                md5 = "-"
            elif category == "archive":
                target = row.get("target", {}).get("human", "-")
                target_type = "Archive"
                md5 = "-"
            else:
                target = None
                target_type = "Not Detected"
                md5 = "-"

            score = info.get("score")
            if score < 4:
                badge_type = "badge-default"
            elif score < 7:
                badge_type = "badge-warning"
            else:
                badge_type = "badge-danger"

            tasks[len(tasks)] = {
                "id": info["id"],
                "target": target,
                "md5": md5,
                "category": category,
                "added_on": info.get("added"),
                "completed_on": info.get("ended"),
                "status": "reported",
                "score": info.get("score"),
                "badge_type": badge_type,
                "target_type": target_type
            }
        return JsonResponse({
            "tasks": sorted(
                tasks.values(), key=lambda task: task["id"], reverse=True
            ),
        }, safe=False)

    @api_post
    def tasks_stats(request, body):
        """
        Fetches the number of analysis over a
        given period for the "failed" and
        "successful" states. Values are
        returned in months.
        :param days: integer; the amount of days to go back in time starting from today.
        :return: A list of months and their statistics
        """
        now = datetime.datetime.now()
        days = body.get("days", 365)

        if not isinstance(days, int):
            return json_error_response("parameter \"days\" not an integer")

        q = db.Session().query(Task)
        q = q.filter(Task.added_on.between(
            now - datetime.timedelta(days=days), now)
        )
        q = q.order_by(sqlalchemy.asc(Task.added_on))
        tasks = q.all()

        def _rtn_structure(start):
            _data = []

            for i in range(0, 12):
                if (now - start).total_seconds() < 0:
                    return _data

                _data.append({
                    "month": start.month,
                    "year": start.year,
                    "month_human": calendar.month_name[start.month],
                    "num": 0
                })

                start = start + dateutil.relativedelta.relativedelta(months=1)

            return _data

        if not tasks:
            return json_error_response("No tasks found")

        data = {
            "analysis": _rtn_structure(tasks[0].added_on),
            "failed": _rtn_structure(tasks[0].added_on)
        }

        for task in tasks:
            added_on = task.added_on
            success = "analysis" if task.status == "reported" else "failed"

            entry = next((z for z in data[success] if
                          z["month"] == added_on.month and
                          z["year"] == added_on.year), None)
            if entry:
                entry["num"] += 1

        return JsonResponse({"status": True, "data": data}, safe=False)

    @api_post
    def feedback_send(request, body):
        f = CuckooFeedback()

        task_id = body.get("task_id")
        if task_id and task_id.isdigit():
            task_id = int(task_id)

        try:
            feedback_id = f.send_form(
                task_id=task_id,
                name=body.get("name"),
                company=body.get("company"),
                email=body.get("email"),
                message=body.get("message"),
                json_report=body.get("include_analysis", False),
                memdump=body.get("include_memdump", False)
            )
        except CuckooFeedbackError as e:
            return json_error_response(str(e))

        return JsonResponse({
            "status": True,
            "feedback_id": feedback_id,
        }, safe=False)


    @api_get
    def statistics_month(requests):
        current_date = datetime.datetime(2017, 6, 18)

        statictis_list = []
        k = date_to_label(current_date)
        v = get_day_statistics(current_date)
        statictis_list.append((k, v))
        for i in range(30):
            current_date -= datetime.timedelta(days=1)
            k = date_to_label(current_date)
            v = get_day_statistics(current_date)
            statictis_list.append((k, v))
        statictis_list.reverse()

        result = dict()
        json = dict()
        json["x"] = []
        json["low"] = []
        json["medium"] = []
        json["high"] = []
        for k, v in statictis_list:
            json["x"].append(k)
            json["low"].append(v["threat"]["low"])
            json["medium"].append(v["threat"]["medium"])
            json["high"].append(v["threat"]["high"])
        result["analysis_num"] = json

        def get_period_sum(period):
            json = dict()
            json["low"] = 0
            json["medium"] = 0
            json["high"] = 0
            for k, v in period:
                json["low"] += v["threat"]["low"]
                json["medium"] += v["threat"]["medium"]
                json["high"] += v["threat"]["high"]
            return json

        result["today_num"] = get_period_sum(statictis_list[-1:])
        result["this_week_num"] = get_period_sum(statictis_list[-7:])
        result["this_month_num"] = get_period_sum(statictis_list)

        return JsonResponse(
            result,
            safe=True
        )


def date_to_label(date):
    return "%d/%d" % (date.month, date.day)


def date_to_key(date):
    return date.strftime("%Y-%m-%d")


STATISTICS_CACHE = dict()
TODAY_STATISTICS_CACHE = None


def get_day_statistics(date):
    global TODAY_STATISTICS_CACHE
    global STATISTICS_CACHE
    current_date = datetime.datetime.now()
    today_key = date_to_key(current_date)
    key = date_to_key(date)

    # today's data
    if today_key == key:
        if TODAY_STATISTICS_CACHE:
            cache_time, v = TODAY_STATISTICS_CACHE
            expire_time = datetime.datetime.now() - cache_time
            if expire_time > datetime.timedelta(minutes=1):
                print "Today cache expire"
                TODAY_STATISTICS_CACHE = (current_date, gen_statistics(date))
            else:
                print "Today cache hit"
        else:
            print "Today cache miss"
            TODAY_STATISTICS_CACHE = (current_date, gen_statistics(date))

        return TODAY_STATISTICS_CACHE[1]

    obj = STATISTICS_CACHE.get(key, None)

    if obj:
        return obj
    else:
        doc = mongo.db.statistics.find_one(
            {"key": key}
        )
        if doc:
            STATISTICS_CACHE[key] = doc["value"]
            return doc["value"]
        else:
            v = gen_statistics(date)
            STATISTICS_CACHE[key] = v
            mongo.db.statistics.insert_one(
                {
                    "key": key,
                    "value": v
                }
            )
            return v


def gen_statistics(date):
    start_time = copy.copy(date)
    end_time = copy.copy(date)
    end_time += datetime.timedelta(days=1)

    cursor = mongo.db.analysis.find(
        {
            "info.added":
                {
                    "$gte": start_time,
                    "$lt": end_time,
                }
        },
        ["info", "target"],
        sort=[("_id", pymongo.DESCENDING)]
    )

    s = {
        "threat": {
            "low": 0,
            "medium": 0,
            "high": 0
        },
        "type": {
        }
    }

    for doc in cursor:
        score = doc.get("info", {}).get("score", 0)
        score = float(score)
        if score < 4:
            add_one(s["threat"], "low")
        elif score < 7:
            add_one(s["threat"], "medium")
        else:
            add_one(s["threat"], "high")

        category = doc.get("info", {}).get("category")
        if category == "file":
            target_type = doc.get("target", {}).get("file", {}).get("type", "Not detected")
        elif category == "url":
            target_type = "URL"
        elif category == "archive":
            target_type = doc.get("target", {}).get("file", {}).get("type", "Not detected")
        else:
            target_type = "Not Detected"
        type_name = parse_type(target_type)
        add_one(s["type"], type_name)

    return s


def add_one(d, k):
    d[k] = d.get(k, 0) + 1


ANALYSIS_TYPE = [
    ("ASCII",       re.compile(r".*(ASCII)")),
    ("HTML",        re.compile(r".*(HTML)")),
    ("JAR",         re.compile(r".*(JAR)")),
    ("Document",    re.compile(r".*(Excel|Word|PowerPoint|PDF)")),
    ("PE",          re.compile(r".*(PE32|executable)")),
    ("URL",         re.compile(r".*(URL)")),
    ("ZIP",         re.compile(r".*(Zip)")),
]


def parse_type(t):
    for name, r in ANALYSIS_TYPE:
        m = r.match(t)
        if m:
            return name
    return "Other"
