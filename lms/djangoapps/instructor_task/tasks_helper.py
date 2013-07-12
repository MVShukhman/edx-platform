"""
This file contains tasks that are designed to perform background operations on the
running state of a course.

"""
import json
from json import JSONEncoder
from time import time
from sys import exc_info
from traceback import format_exc
from os.path import exists
import meliae.scanner as scanner

from celery import Task, current_task
from celery.utils.log import get_task_logger
from celery.states import SUCCESS, FAILURE

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction, reset_queries
from dogapi import dog_stats_api

from xmodule.course_module import CourseDescriptor
from xmodule.modulestore.django import modulestore

from track.views import task_track

from courseware.grades import grade_as_task, GradingModuleInstantiationException
from courseware.models import StudentModule, OfflineComputedGrade
from courseware.model_data import FieldDataCache
from courseware.module_render import get_module_for_descriptor_internal
from instructor_task.models import InstructorTask, PROGRESS

# define different loggers for use within tasks and on client side
TASK_LOG = get_task_logger(__name__)

# define value to use when no task_id is provided:
UNKNOWN_TASK_ID = 'unknown-task_id'

# define values for update functions to use to return status to perform_module_state_update
UPDATE_STATUS_SUCCEEDED = 'succeeded'
UPDATE_STATUS_FAILED = 'failed'
UPDATE_STATUS_SKIPPED = 'skipped'


class BaseInstructorTask(Task):
    """
    Base task class for use with InstructorTask models.

    Permits updating information about task in corresponding InstructorTask for monitoring purposes.

    Assumes that the entry_id of the InstructorTask model is the first argument to the task.

    The `entry_id` is the primary key for the InstructorTask entry representing the task.  This class
    updates the entry on success and failure of the task it wraps.  It is setting the entry's value
    for task_state based on what Celery would set it to once the task returns to Celery:
    FAILURE if an exception is encountered, and SUCCESS if it returns normally.
    Other arguments are pass-throughs to perform_module_state_update, and documented there.
    """
    abstract = True

    def on_success(self, task_progress, task_id, args, kwargs):
        """
        Update InstructorTask object corresponding to this task with info about success.

        Updates task_output and task_state.  But it shouldn't actually do anything
        if the task is only creating subtasks to actually do the work.

        Assumes `task_progress` is a dict containing the task's result, with the following keys:

          'attempted': number of attempts made
          'succeeded': number of attempts that "succeeded"
          'skipped': number of attempts that "skipped"
          'failed': number of attempts that "failed"
          'total': number of possible subtasks to attempt
          'action_name': user-visible verb to use in status messages.  Should be past-tense.
              Pass-through of input `action_name`.
          'duration_ms': how long the task has (or had) been running.

        This is JSON-serialized and stored in the task_output column of the InstructorTask entry.

        """
        TASK_LOG.debug('Task %s: success returned with progress: %s', task_id, task_progress)
        # We should be able to find the InstructorTask object to update
        # based on the task_id here, without having to dig into the
        # original args to the task.  On the other hand, the entry_id
        # is the first value passed to all such args, so we'll use that.
        # And we assume that it exists, else we would already have had a failure.
        entry_id = args[0]
        entry = InstructorTask.objects.get(pk=entry_id)
        # Check to see if any subtasks had been defined as part of this task.
        # If not, then we know that we're done.  (If so, let the subtasks
        # handle updating task_state themselves.)
        if len(entry.subtasks) == 0:
            entry.task_output = InstructorTask.create_output_for_success(task_progress)
            entry.task_state = SUCCESS
            entry.save_now()

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """
        Update InstructorTask object corresponding to this task with info about failure.

        Fetches and updates exception and traceback information on failure.

        If an exception is raised internal to the task, it is caught by celery and provided here.
        The information is recorded in the InstructorTask object as a JSON-serialized dict
        stored in the task_output column.  It contains the following keys:

               'exception':  type of exception object
               'message': error message from exception object
               'traceback': traceback information (truncated if necessary)

        Note that there is no way to record progress made within the task (e.g. attempted,
        succeeded, etc.) when such failures occur.
        """
        TASK_LOG.debug('Task %s: failure returned', task_id)
        entry_id = args[0]
        try:
            entry = InstructorTask.objects.get(pk=entry_id)
        except InstructorTask.DoesNotExist:
            # if the InstructorTask object does not exist, then there's no point
            # trying to update it.
            TASK_LOG.error("Task (%s) has no InstructorTask object for id %s", task_id, entry_id)
        else:
            TASK_LOG.warning("Task (%s) failed: %s %s", task_id, einfo.exception, einfo.traceback)
            entry.task_output = InstructorTask.create_output_for_failure(einfo.exception, einfo.traceback)
            entry.task_state = FAILURE
            entry.save_now()


class UpdateProblemModuleStateError(Exception):
    """
    Error signaling a fatal condition while updating problem modules.

    Used when the current module cannot be processed and no more
    modules should be attempted.
    """
    pass


def _get_current_task():
    """
    Stub to make it easier to test without actually running Celery.

    This is a wrapper around celery.current_task, which provides access
    to the top of the stack of Celery's tasks.  When running tests, however,
    it doesn't seem to work to mock current_task directly, so this wrapper
    is used to provide a hook to mock in tests, while providing the real
    `current_task` in production.
    """
    return current_task


def run_main_task(entry_id, task_fcn, action_name):
    """
    Applies the `task_fcn` to the arguments defined in `entry_id` InstructorTask.

    Arguments passed to `task_fcn` are:

     `entry_id` : the primary key for the InstructorTask entry representing the task.
     `course_id` : the id for the course.
     `task_input` : dict containing task-specific arguments, JSON-decoded from InstructorTask's task_input.
     `action_name` : past-tense verb to use for constructing status messages.

    If no exceptions are raised, the `task_fcn` should return a dict containing
    the task's result with the following keys:

          'attempted': number of attempts made
          'succeeded': number of attempts that "succeeded"
          'skipped': number of attempts that "skipped"
          'failed': number of attempts that "failed"
          'total': number of possible subtasks to attempt
          'action_name': user-visible verb to use in status messages.
              Should be past-tense.  Pass-through of input `action_name`.
          'duration_ms': how long the task has (or had) been running.

    """

    # get the InstructorTask to be updated.  If this fails, then let the exception return to Celery.
    # There's no point in catching it here.
    entry = InstructorTask.objects.get(pk=entry_id)

    # get inputs to use in this task from the entry:
    task_id = entry.task_id
    course_id = entry.course_id
    task_input = json.loads(entry.task_input)

    # construct log message:
    fmt = 'task "{task_id}": course "{course_id}" input "{task_input}"'
    task_info_string = fmt.format(task_id=task_id, course_id=course_id, task_input=task_input)

    TASK_LOG.info('Starting update (nothing %s yet): %s', action_name, task_info_string)

    # Check that the task_id submitted in the InstructorTask matches the current task
    # that is running.
    request_task_id = _get_current_task().request.id
    if task_id != request_task_id:
        fmt = 'Requested task did not match actual task "{actual_id}": {task_info}'
        message = fmt.format(actual_id=request_task_id, task_info=task_info_string)
        TASK_LOG.error(message)
        raise ValueError(message)

    # Now do the work:
    with dog_stats_api.timer('instructor_tasks.time.overall', tags=['action:{name}'.format(name=action_name)]):
        task_progress = task_fcn(entry_id, course_id, task_input, action_name)

    # Release any queries that the connection has been hanging onto:
    reset_queries()

    # write out a dump of memory usage at the end of this, to see what is left
    # around.  Enable it if it hasn't been explicitly disabled.
    if action_name == 'graded' and getattr(settings, 'PERFORM_TASK_MEMORY_DUMP', True):
        filename = "meliae_dump_{}.dat".format(task_id)
        # Hardcode the name of a dump directory to try to use.
        # If if doesn't exist, just continue to use the "local" directory.
        dirname = '/mnt/memdump/'
        if exists(dirname):
            filename = dirname + filename
        TASK_LOG.info('Dumping memory information to %s', filename)
        scanner.dump_all_objects(filename)

    # log and exit, returning task_progress info as task result:
    TASK_LOG.info('Finishing %s: final: %s', task_info_string, task_progress)
    return task_progress


def perform_enrolled_student_update(course_id, _module_state_key, student_identifier, update_fcn, action_name, filter_fcn):
    """
    """
    # Throw an exception if _module_state_key is specified, because that's not meaningful here
    if _module_state_key is not None:
        raise ValueError("Value for problem_url not expected")

    # Get start time for task:
    start_time = time()

    # Find the course descriptor.
    # Depth is set to zero, to indicate that the number of levels of children
    # for the modulestore to cache should be infinite.  If the course is not found,
    # let it throw the exception.
    course_loc = CourseDescriptor.id_to_location(course_id)
    course_descriptor = modulestore().get_instance(course_id, course_loc, depth=0)

    enrolled_students = User.objects.filter(courseenrollment__course_id=course_id).prefetch_related("groups").order_by('username')

    # Give the option of updating an individual student. If not specified,
    # then updates all students who have enrolled in the course
    student = None
    if student_identifier is not None:
        # if an identifier is supplied, then look for the student,
        # and let it throw an exception if none is found.
        if "@" in student_identifier:
            student = User.objects.get(email=student_identifier)
        elif student_identifier is not None:
            student = User.objects.get(username=student_identifier)

    if student is not None:
        enrolled_students = enrolled_students.filter(id=student.id)

    if filter_fcn is not None:
        enrolled_students = filter_fcn(enrolled_students)

    # perform the main loop
    num_updated = 0
    num_attempted = 0
    num_total = enrolled_students.count()

    def get_task_progress():
        """Return a dict containing info about current task"""
        current_time = time()
        progress = {'action_name': action_name,
                    'attempted': num_attempted,
                    'updated': num_updated,
                    'total': num_total,
                    'duration_ms': int((current_time - start_time) * 1000),
                    }
        return progress

    task_progress = get_task_progress()
    _get_current_task().update_state(state=PROGRESS, meta=task_progress)
    for enrolled_student in enrolled_students:
        num_attempted += 1
        # There is no try here:  if there's an error, we let it throw, and the task will
        # be marked as FAILED, with a stack trace.
        with dog_stats_api.timer('instructor_tasks.student.time.step', tags=['action:{name}'.format(name=action_name)]):
            if update_fcn(course_descriptor, enrolled_student):
                # If the update_fcn returns true, then it performed some kind of work.
                # Logging of failures is left to the update_fcn itself.
                num_updated += 1

        # update task status:
        task_progress = get_task_progress()
        _get_current_task().update_state(state=PROGRESS, meta=task_progress)

       # add temporary hack to make grading tasks finish more quickly!
       # TODO: REMOVE THIS when done with debugging
       if num_attempted == 1000:
           break;

    return task_progress


def perform_module_state_update(course_id, module_state_key, student_identifier, update_fcn, action_name, filter_fcn):
    """
    Performs generic update by visiting StudentModule instances with the update_fcn provided.

    StudentModule instances are those that match the specified `course_id` and `module_state_key`.
    If `student_identifier` is not None, it is used as an additional filter to limit the modules to those belonging
    to that student. If `student_identifier` is None, performs update on modules for all students on the specified problem.

    If a `filter_fcn` is not None, it is applied to the query that has been constructed.  It takes one
    argument, which is the query being filtered, and returns the filtered version of the query.

    The `update_fcn` is called on each StudentModule that passes the resulting filtering.
    It is passed three arguments:  the module_descriptor for the module pointed to by the
    module_state_key, the particular StudentModule to update, and the xmodule_instance_args being
    passed through.  If the value returned by the update function evaluates to a boolean True,
    the update is successful; False indicates the update on the particular student module failed.
    A raised exception indicates a fatal condition -- that no other student modules should be considered.

    The return value is a dict containing the task's results, with the following keys:

          'attempted': number of attempts made
          'succeeded': number of attempts that "succeeded"
          'skipped': number of attempts that "skipped"
          'failed': number of attempts that "failed"
          'total': number of possible updates to attempt
          'action_name': user-visible verb to use in status messages.  Should be past-tense.
              Pass-through of input `action_name`.
          'duration_ms': how long the task has (or had) been running.

    Because this is run internal to a task, it does not catch exceptions.  These are allowed to pass up to the
    next level, so that it can set the failure modes and capture the error trace in the InstructorTask and the
    result object.

    """
    # get start time for task:
    start_time = time()

    module_state_key = task_input.get('problem_url')
    student_identifier = task_input.get('student')

    # find the problem descriptor:
    module_descriptor = modulestore().get_instance(course_id, module_state_key)

    # find the module in question
    modules_to_update = StudentModule.objects.filter(course_id=course_id,
                                                     module_state_key=module_state_key)

    # give the option of updating an individual student. If not specified,
    # then updates all students who have responded to a problem so far
    student = None
    if student_identifier is not None:
        # if an identifier is supplied, then look for the student,
        # and let it throw an exception if none is found.
        if "@" in student_identifier:
            student = User.objects.get(email=student_identifier)
        elif student_identifier is not None:
            student = User.objects.get(username=student_identifier)

    if student is not None:
        modules_to_update = modules_to_update.filter(student_id=student.id)

    if filter_fcn is not None:
        modules_to_update = filter_fcn(modules_to_update)

    # perform the main loop
    num_attempted = 0
    num_succeeded = 0
    num_skipped = 0
    num_failed = 0
    num_total = modules_to_update.count()

    def get_task_progress():
        """Return a dict containing info about current task"""
        current_time = time()
        progress = {'action_name': action_name,
                    'attempted': num_attempted,
                    'succeeded': num_succeeded,
                    'skipped': num_skipped,
                    'failed': num_failed,
                    'total': num_total,
                    'duration_ms': int((current_time - start_time) * 1000),
                    }
        return progress

    task_progress = get_task_progress()
    _get_current_task().update_state(state=PROGRESS, meta=task_progress)
    for module_to_update in modules_to_update:
        num_attempted += 1
        # There is no try here:  if there's an error, we let it throw, and the task will
        # be marked as FAILED, with a stack trace.
        with dog_stats_api.timer('instructor_tasks.module.time.step', tags=['action:{name}'.format(name=action_name)]):
            update_status = update_fcn(module_descriptor, module_to_update)
            if update_status == UPDATE_STATUS_SUCCEEDED:
                # If the update_fcn returns true, then it performed some kind of work.
                # Logging of failures is left to the update_fcn itself.
                num_succeeded += 1
            elif update_status == UPDATE_STATUS_FAILED:
                num_failed += 1
            elif update_status == UPDATE_STATUS_SKIPPED:
                num_skipped += 1
            else:
                raise UpdateProblemModuleStateError("Unexpected update_status returned: {}".format(update_status))

        # update task status:
        task_progress = get_task_progress()
        _get_current_task().update_state(state=PROGRESS, meta=task_progress)

    return task_progress

def _get_task_id_from_xmodule_args(xmodule_instance_args):
    """Gets task_id from `xmodule_instance_args` dict, or returns default value if missing."""
    return xmodule_instance_args.get('task_id', UNKNOWN_TASK_ID) if xmodule_instance_args is not None else UNKNOWN_TASK_ID

def run_update_task(entry_id, visit_fcn, update_fcn, action_name, filter_fcn):
    """
    TODO: UPDATE THIS DOCSTRING

    Performs generic update by visiting StudentModule instances with the update_fcn provided.

    The `entry_id` is the primary key for the InstructorTask entry representing the task.  This function
    updates the entry on success and failure of the perform_module_state_update function it
    wraps.  It is setting the entry's value for task_state based on what Celery would set it to once
    the task returns to Celery:  FAILURE if an exception is encountered, and SUCCESS if it returns normally.
    Other arguments are pass-throughs to perform_module_state_update, and documented there.

    If no exceptions are raised, a dict containing the task's result is returned, with the following keys:

          'attempted': number of attempts made
          'updated': number of attempts that "succeeded"
          'total': number of possible subtasks to attempt
          'action_name': user-visible verb to use in status messages.  Should be past-tense.
              Pass-through of input `action_name`.
          'duration_ms': how long the task has (or had) been running.

    Before returning, this is also JSON-serialized and stored in the task_output column of the InstructorTask entry.
    """
    # get the InstructorTask to be updated.  If this fails, then let the exception return to Celery.
    # There's no point in catching it here.
    entry = InstructorTask.objects.get(pk=entry_id)

    # get inputs to use in this task from the entry:
    task_id = entry.task_id
    course_id = entry.course_id
    task_input = json.loads(entry.task_input)
    module_state_key = task_input.get('problem_url')
    student_ident = task_input.get('student')

    # construct log message:
    fmt = 'task "{task_id}": course "{course_id}" problem "{state_key}"'
    task_info_string = fmt.format(task_id=task_id, course_id=course_id, state_key=module_state_key)

    TASK_LOG.info('Starting update (nothing %s yet): %s', action_name, task_info_string)

    # Now that we have an entry we can try to catch failures:
    task_progress = None
    try:
        # Check that the task_id submitted in the InstructorTask matches the current task
        # that is running.
        request_task_id = _get_current_task().request.id
        if task_id != request_task_id:
            fmt = 'Requested task did not match actual task "{actual_id}": {task_info}'
            message = fmt.format(actual_id=request_task_id, task_info=task_info_string)
            TASK_LOG.error(message)
            raise UpdateProblemModuleStateError(message)

        # Now do the work:
        with dog_stats_api.timer('instructor_tasks.time.overall', tags=['action:{name}'.format(name=action_name)]):
            task_progress = visit_fcn(course_id, module_state_key, student_ident, update_fcn, action_name, filter_fcn)
        # If we get here, we assume we've succeeded, so update the InstructorTask entry in anticipation.
        # But we do this within the try, in case creating the task_output causes an exception to be
        # raised.
        entry.task_output = InstructorTask.create_output_for_success(task_progress)
        entry.task_state = SUCCESS
        entry.save_now()

    except Exception:
        # try to write out the failure to the entry before failing
        _, exception, traceback = exc_info()
        traceback_string = format_exc(traceback) if traceback is not None else ''
        TASK_LOG.warning("background task (%s) failed: %s %s", task_id, exception, traceback_string)
        entry.task_output = InstructorTask.create_output_for_failure(exception, traceback_string)
        entry.task_state = FAILURE
        entry.save_now()
        raise

    # Release any queries that the connection has been hanging onto:
    reset_queries()

    # log and exit, returning task_progress info as task result:
    TASK_LOG.info('Finishing %s: final: %s', task_info_string, task_progress)
    return task_progress

def _get_xqueue_callback_url_prefix(xmodule_instance_args):
    """Gets prefix to use when constructing xqueue_callback_url."""
    return xmodule_instance_args.get('xqueue_callback_url_prefix', '') if xmodule_instance_args is not None else ''


def _get_track_function_for_task(student, xmodule_instance_args=None, source_page='x_module_task'):
    """
    Make a tracking function that logs what happened.

    For insertion into ModuleSystem, and used by CapaModule, which will
    provide the event_type (as string) and event (as dict) as arguments.
    The request_info and task_info (and page) are provided here.
    """
    # get request-related tracking information from args passthrough, and supplement with task-specific
    # information:
    request_info = xmodule_instance_args.get('request_info', {}) if xmodule_instance_args is not None else {}
    task_info = {'student': student.username, 'task_id': _get_task_id_from_xmodule_args(xmodule_instance_args)}

    return lambda event_type, event: task_track(request_info, task_info, event_type, event, page=source_page)


def _get_xqueue_callback_url_prefix(xmodule_instance_args):
    """

    """
    return xmodule_instance_args.get('xqueue_callback_url_prefix', '') if xmodule_instance_args is not None else ''


def _get_track_function_for_task(student, xmodule_instance_args=None, source_page='x_module_task'):
    """
    Make a tracking function that logs what happened.

    For insertion into ModuleSystem, and used by CapaModule, which will
    provide the event_type (as string) and event (as dict) as arguments.
    The request_info and task_info (and page) are provided here.
    """
    # get request-related tracking information from args passthrough, and supplement with task-specific
    # information:
    request_info = xmodule_instance_args.get('request_info', {}) if xmodule_instance_args is not None else {}
    task_info = {'student': student.username, 'task_id': _get_task_id_from_xmodule_args(xmodule_instance_args)}

    return lambda event_type, event: task_track(request_info, task_info, event_type, event, page=source_page)


def _get_module_instance_for_task(course_id, student, module_descriptor, xmodule_instance_args=None,
                                  grade_bucket_type=None):
    """
    Fetches a StudentModule instance for a given `course_id`, `student` object, and `module_descriptor`.

    `xmodule_instance_args` is used to provide information for creating a track function and an XQueue callback.
    These are passed, along with `grade_bucket_type`, to get_module_for_descriptor_internal, which sidesteps
    the need for a Request object when instantiating an xmodule instance.
    """
    # reconstitute the problem's corresponding XModule:
    field_data_cache = FieldDataCache.cache_for_descriptor_descendents(course_id, student, module_descriptor)

    return get_module_for_descriptor_internal(student, module_descriptor, field_data_cache, course_id,
                                              _get_track_function_for_task(student, xmodule_instance_args),
                                              _get_xqueue_callback_url_prefix(xmodule_instance_args),
                                              grade_bucket_type=grade_bucket_type)


@transaction.autocommit
def rescore_problem_module_state(xmodule_instance_args, module_descriptor, student_module):
    '''
    Takes an XModule descriptor and a corresponding StudentModule object, and
    performs rescoring on the student's problem submission.

    Throws exceptions if the rescoring is fatal and should be aborted if in a loop.
    In particular, raises UpdateProblemModuleStateError if module fails to instantiate,
    or if the module doesn't support rescoring.

    Returns True if problem was successfully rescored for the given student, and False
    if problem encountered some kind of error in rescoring.
    '''
    # unpack the StudentModule:
    course_id = student_module.course_id
    student = student_module.student
    module_state_key = student_module.module_state_key
    instance = _get_module_instance_for_task(course_id, student, module_descriptor, xmodule_instance_args, grade_bucket_type='rescore')

    if instance is None:
        # Either permissions just changed, or someone is trying to be clever
        # and load something they shouldn't have access to.
        msg = "No module {loc} for student {student}--access denied?".format(loc=module_state_key,
                                                                             student=student)
        TASK_LOG.debug(msg)
        raise UpdateProblemModuleStateError(msg)

    if not hasattr(instance, 'rescore_problem'):
        # This should also not happen, since it should be already checked in the caller,
        # but check here to be sure.
        msg = "Specified problem does not support rescoring."
        raise UpdateProblemModuleStateError(msg)

    result = instance.rescore_problem()
    instance.save()
    if 'success' not in result:
        # don't consider these fatal, but false means that the individual call didn't complete:
        TASK_LOG.warning(u"error processing rescore call for course {course}, problem {loc} and student {student}: "
                         "unexpected response {msg}".format(msg=result, course=course_id, loc=module_state_key, student=student))
        return UPDATE_STATUS_FAILED
    elif result['success'] not in ['correct', 'incorrect']:
        TASK_LOG.warning(u"error processing rescore call for course {course}, problem {loc} and student {student}: "
                         "{msg}".format(msg=result['success'], course=course_id, loc=module_state_key, student=student))
        return UPDATE_STATUS_FAILED
    else:
        TASK_LOG.debug(u"successfully processed rescore call for course {course}, problem {loc} and student {student}: "
                       "{msg}".format(msg=result['success'], course=course_id, loc=module_state_key, student=student))
        return UPDATE_STATUS_SUCCEEDED


@transaction.autocommit
def reset_attempts_module_state(xmodule_instance_args, _module_descriptor, student_module):
    """
    Resets problem attempts to zero for specified `student_module`.

    Returns a status of UPDATE_STATUS_SUCCEEDED if a problem has non-zero attempts
    that are being reset, and UPDATE_STATUS_SKIPPED otherwise.
    """
    update_status = UPDATE_STATUS_SKIPPED
    problem_state = json.loads(student_module.state) if student_module.state else {}
    if 'attempts' in problem_state:
        old_number_of_attempts = problem_state["attempts"]
        if old_number_of_attempts > 0:
            problem_state["attempts"] = 0
            # convert back to json and save
            student_module.state = json.dumps(problem_state)
            student_module.save()
            # get request-related tracking information from args passthrough,
            # and supplement with task-specific information:
            track_function = _get_track_function_for_task(student_module.student, xmodule_instance_args)
            event_info = {"old_attempts": old_number_of_attempts, "new_attempts": 0}
            track_function('problem_reset_attempts', event_info)
            update_status = UPDATE_STATUS_SUCCEEDED

    return update_status


@transaction.autocommit
def delete_problem_module_state(xmodule_instance_args, _module_descriptor, student_module):
    """
    Delete the StudentModule entry.

    Always returns UPDATE_STATUS_SUCCEEDED, indicating success, if it doesn't raise an exception due to database error.
    """
    student_module.delete()
    # get request-related tracking information from args passthrough,
    # and supplement with task-specific information:
    track_function = _get_track_function_for_task(student_module.student, xmodule_instance_args)
    track_function('problem_delete_state', {})
    return UPDATE_STATUS_SUCCEEDED


# def update_students(entry_id, update_fcn, action_name, filter_fcn, xmodule_instance_args):
#     """
#     Performs generic update by visiting StudentModule instances with the update_fcn provided.
#
#     The `entry_id` is the primary key for the InstructorTask entry representing the task.  This function
#     updates the entry on success and failure of the _perform_module_state_update function it
#     wraps.  It is setting the entry's value for task_state based on what Celery would set it to once
#     the task returns to Celery:  FAILURE if an exception is encountered, and SUCCESS if it returns normally.
#     Other arguments are pass-throughs to _perform_module_state_update, and documented there.
#
#     If no exceptions are raised, a dict containing the task's result is returned, with the following keys:
#
#           'attempted': number of attempts made
#           'updated': number of attempts that "succeeded"
#           'total': number of possible subtasks to attempt
#           'action_name': user-visible verb to use in status messages.  Should be past-tense.
#               Pass-through of input `action_name`.
#           'duration_ms': how long the task has (or had) been running.
#
#     Before returning, this is also JSON-serialized and stored in the task_output column of the InstructorTask entry.
#
#     If an exception is raised internally, it is caught and recorded in the InstructorTask entry.
#     This is also a JSON-serialized dict, stored in the task_output column, containing the following keys:
#
#            'exception':  type of exception object
#            'message': error message from exception object
#            'traceback': traceback information (truncated if necessary)
#
#     Once the exception is caught, it is raised again and allowed to pass up to the
#     task-running level, so that it can also set the failure modes and capture the error trace in the
#     result object that Celery creates.
#
#     """
#
#     # get the InstructorTask to be updated.  If this fails, then let the exception return to Celery.
#     # There's no point in catching it here.
#     entry = InstructorTask.objects.get(pk=entry_id)
#
#     # get inputs to use in this task from the entry:
#     task_id = entry.task_id
#     course_id = entry.course_id
#
#     # TODO: no input expected.  Should we check?
#     # task_input = json.loads(entry.task_input)
#     # module_state_key = task_input.get('problem_url')
#     # student_ident = task_input['student'] if 'student' in task_input else None
#     # fmt = 'Starting to update problem modules as task "{task_id}": course "{course_id}" problem "{state_key}": nothing {action} yet'
#     # TASK_LOG.info(fmt.format(task_id=task_id, course_id=course_id, state_key=module_state_key, action=action_name))
#     fmt = 'Starting to update students as task "{task_id}": course "{course_id}": nothing {action} yet'
#     TASK_LOG.info(fmt.format(task_id=task_id, course_id=course_id, action=action_name))
#
#     # Now that we have an entry we can try to catch failures:
#     task_progress = None
#     try:
#         # Check that the task_id submitted in the InstructorTask matches the current task
#         # that is running.
#         request_task_id = _get_current_task().request.id
#         if task_id != request_task_id:
#             # TODO: Provide course information here?!
#             fmt = 'Requested task "{task_id}" did not match actual task "{actual_id}"'
#             # TODO: state_key not in message!
#             # message = fmt.format(task_id=task_id, course_id=course_id, state_key=module_state_key, actual_id=request_task_id)
#             message = fmt.format(task_id=task_id, course_id=course_id, actual_id=request_task_id)
#             TASK_LOG.error(message)
#             raise UpdateProblemModuleStateError(message)
#
#         # Now do the work:
#         with dog_stats_api.timer('instructor_tasks.module.time.overall', tags=['action:{name}'.format(name=action_name)]):
#             task_progress = _perform_module_state_update(course_id, module_state_key, student_ident, update_fcn,
#                                                          action_name, filter_fcn, xmodule_instance_args)
#         # If we get here, we assume we've succeeded, so update the InstructorTask entry in anticipation.
#         # But we do this within the try, in case creating the task_output causes an exception to be
#         # raised.
#         entry.task_output = InstructorTask.create_output_for_success(task_progress)
#         entry.task_state = SUCCESS
#         entry.save_now()
#
#     except Exception:
#         # try to write out the failure to the entry before failing
#         _, exception, traceback = exc_info()
#         traceback_string = format_exc(traceback) if traceback is not None else ''
#         TASK_LOG.warning("background task (%s) failed: %s %s", task_id, exception, traceback_string)
#         entry.task_output = InstructorTask.create_output_for_failure(exception, traceback_string)
#         entry.task_state = FAILURE
#         entry.save_now()
#         raise
#
#     # log and exit, returning task_progress info as task result:
#     fmt = 'Finishing task "{task_id}": course "{course_id}" problem "{state_key}": final: {progress}'
#     TASK_LOG.info(fmt.format(task_id=task_id, course_id=course_id, state_key=module_state_key, progress=task_progress))
#     return task_progress
#
#
# import json
# import time
#
# from json import JSONEncoder
# from courseware import grades, models
# from courseware.courses import get_course_by_id
# from django.contrib.auth.models import User
#
#
# class MyEncoder(JSONEncoder):
#
#     def _iterencode(self, obj, markers=None):
#         if isinstance(obj, tuple) and hasattr(obj, '_asdict'):
#             gen = self._iterencode_dict(obj._asdict(), markers)
#         else:
#             gen = JSONEncoder._iterencode(self, obj, markers)
#         for chunk in gen:
#             yield chunk
#
#
# def offline_grade_calculation(course_id):
#     '''
#     Compute grades for all students for a specified course, and save results to the DB.
#     '''
#
#     tstart = time.time()
#     enrolled_students = User.objects.filter(courseenrollment__course_id=course_id).prefetch_related("groups").order_by('username')
#
#     enc = MyEncoder()
#
#     class DummyRequest(object):
#         META = {}
#         def __init__(self):
#             return
#         def get_host(self):
#             return 'edx.mit.edu'
#         def is_secure(self):
#             return False
#
#     request = DummyRequest()
#
#     print "%d enrolled students" % len(enrolled_students)
#     course = get_course_by_id(course_id)
#
#     for student in enrolled_students:
#         gradeset = grades.grade(student, request, course, keep_raw_scores=True)
#         gs = enc.encode(gradeset)
#         ocg, created = models.OfflineComputedGrade.objects.get_or_create(user=student, course_id=course_id)
#         ocg.gradeset = gs
#         ocg.save()
#         print "%s done" % student      # print statement used because this is run by a management command
#
#     tend = time.time()
#     dt = tend - tstart
#
#     ocgl = models.OfflineComputedGradeLog(course_id=course_id, seconds=dt, nstudents=len(enrolled_students))
#     ocgl.save()
#     print ocgl
#     print "All Done!"


class GradingJSONEncoder(JSONEncoder):

    def _iterencode(self, obj, markers=None):
        if isinstance(obj, tuple) and hasattr(obj, '_asdict'):
            gen = self._iterencode_dict(obj._asdict(), markers)
        else:
            gen = JSONEncoder._iterencode(self, obj, markers)
        for chunk in gen:
            yield chunk


@transaction.autocommit
def update_offline_grade(xmodule_instance_args, course_descriptor, student):
    """
    Update the grading information stored for a particular student in a course.

    Always returns true, indicating success, if it doesn't raise an exception due to database error.
    """
    return_value = True
    # TODO: this could be made into a global?  Are there threading issues that
    # might arise if we did that?  Savings by pulling it out of this inner loop?
    json_encoder = GradingJSONEncoder()

    # call the main grading function:
    track_function = _get_track_function_for_task(student, xmodule_instance_args)
    xqueue_callback_url_prefix = _get_xqueue_callback_url_prefix(xmodule_instance_args)
    try:
        gradeset = grade_as_task(student, course_descriptor, track_function, xqueue_callback_url_prefix)
    except GradingModuleInstantiationException as exc:
        # if we're unable to perform grading because we cannot load one of the student's
        # modules, then just fail this particular student, not the entire grading run.
        TASK_LOG.warning('failing to grade student id="%s" because of module failure: %s', student.id, exc.message)
        return_value = False
    else:
        json_grades = json_encoder.encode(gradeset)
        offline_grade_entry, created = OfflineComputedGrade.objects.get_or_create(user=student, course_id=course_descriptor.id)
        offline_grade_entry.gradeset = json_grades
        offline_grade_entry.save()

        # Get request-related tracking information from args passthrough,
        # and supplement with task-specific information:
        track_function('offline_grade', {'created': created})

    # Release any queries that the connection has been hanging onto:
    reset_queries()
    return return_value
